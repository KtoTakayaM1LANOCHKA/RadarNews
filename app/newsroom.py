"""Persistent discovery sources, Telegram linking and durable editor deliveries."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import feedparser
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from typing import Literal

log = logging.getLogger(__name__)
MAX_FEED_BYTES = 2 * 1024 * 1024


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)


def plain(value, limit=1200):
    parser = TextParser()
    parser.feed(str(value or '')[:20000])
    return re.sub(r'\s+', ' ', html.unescape(' '.join(parser.parts))).strip()[:limit]


def web_url(value):
    try:
        p = urlsplit(value)
        return p.scheme in ('http', 'https') and bool(p.hostname) and not p.username and not p.password and p.port in (None, 80, 443)
    except ValueError:
        return False


async def public_url(value):
    if not web_url(value):
        raise ValueError('Нужен публичный HTTP(S) URL без пароля и нестандартного порта')
    p = urlsplit(value)
    addresses = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Локальные и служебные адреса запрещены')


def search_url(query):
    return 'https://news.google.com/rss/search?' + urlencode({'q': query, 'hl': 'ru', 'gl': 'RU', 'ceid': 'RU:ru'})


async def fetch_feed(client, url, headers=None):
    for _ in range(5):
        await public_url(url)
        async with client.stream('GET', url, headers=headers or {}, follow_redirects=False, timeout=15) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get('location', ''))
                continue
            if response.status_code == 304:
                return None, dict(response.headers)
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_FEED_BYTES:
                    raise ValueError('Лента больше 2 МБ')
            parsed = feedparser.parse(bytes(data))
            if not parsed.get('version'):
                raise ValueError('Источник не является RSS/Atom-лентой')
            return parsed.entries[:100], dict(response.headers)
    raise ValueError('Слишком много перенаправлений')


class SourceIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    kind: Literal['rss', 'search'] = 'search'
    query: str = Field(default='', max_length=180)
    url: str = Field(default='', max_length=900)
    topic: str = Field(default='Город', min_length=1, max_length=60)
    latitude: float = Field(default=55.7519, ge=-90, le=90)
    longitude: float = Field(default=37.6176, ge=-180, le=180)
    interval_minutes: int = Field(default=15, ge=5, le=1440)
    enabled: bool = True

    @model_validator(mode='after')
    def validate_source(self):
        self.name, self.query, self.url = self.name.strip(), self.query.strip(), self.url.strip()
        if len(self.name) < 2 or (self.kind == 'search' and len(self.query) < 3):
            raise ValueError('Укажите название и поисковый запрос от 3 символов')
        if self.kind == 'rss' and not web_url(self.url):
            raise ValueError('Укажите HTTP(S) URL RSS/Atom-ленты')
        return self


class RoleIn(BaseModel):
    role: Literal['user', 'editor']


class TelegramAuth(BaseModel):
    init_data: str = Field(min_length=1, max_length=12000)


class Newsroom:
    def __init__(self, db, auth, editor, make_session, user_view):
        self.db, self.auth, self.editor = db, auth, editor
        self.make_session, self.user_view = make_session, user_view
        self.owner = secrets.token_hex(16)
        self.collect_lock = asyncio.Lock()
        self.router = self.routes()

    def init(self):
        with self.db() as con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS news_sources (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, query TEXT NOT NULL,
              url TEXT NOT NULL, topic TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
              interval_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL, etag TEXT, modified TEXT,
              last_checked TEXT, next_check INTEGER NOT NULL DEFAULT 0, last_error TEXT,
              imported_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_items (
              dedupe_key TEXT PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES news_sources(id),
              event_id INTEGER NOT NULL REFERENCES events(id), created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_links (
              user_id INTEGER PRIMARY KEY REFERENCES users(id), telegram_user_id INTEGER UNIQUE NOT NULL,
              chat_id INTEGER UNIQUE NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS telegram_link_codes (
              code_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id), expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_deliveries (
              id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events(id),
              user_id INTEGER NOT NULL REFERENCES users(id), chat_id INTEGER NOT NULL,
              kind TEXT NOT NULL DEFAULT 'event', submission_id INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              next_try INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL,
              UNIQUE(event_id,user_id,kind,submission_id)
            );
            CREATE INDEX IF NOT EXISTS delivery_pending ON telegram_deliveries(status,next_try);
            CREATE TABLE IF NOT EXISTS worker_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS worker_locks (key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS telegram_messages (
              chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, event_id INTEGER NOT NULL,
              submission_id INTEGER NOT NULL, media_done INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(chat_id,message_id)
            );
            CREATE TRIGGER IF NOT EXISTS notify_new_event AFTER INSERT ON events WHEN NEW.demo=0
            BEGIN
              INSERT OR IGNORE INTO telegram_deliveries(event_id,user_id,chat_id,created_at)
              SELECT NEW.id,l.user_id,l.chat_id,NEW.created_at FROM telegram_links l
              JOIN users u ON u.id=l.user_id WHERE l.enabled=1 AND u.role IN ('editor','admin');
            END;
            CREATE TRIGGER IF NOT EXISTS notify_new_evidence AFTER INSERT ON submissions WHEN NEW.kind IN ('confirm','refute')
            BEGIN
              INSERT OR IGNORE INTO telegram_deliveries(event_id,user_id,chat_id,kind,submission_id,created_at)
              SELECT NEW.event_id,l.user_id,l.chat_id,'evidence',NEW.id,NEW.submitted_at FROM telegram_links l
              JOIN users u ON u.id=l.user_id WHERE l.enabled=1 AND u.role IN ('editor','admin');
            END;
            ''')
            # One editable public-news search; historical demo events are never broadcast.
            if not con.execute('SELECT 1 FROM news_sources LIMIT 1').fetchone():
                con.execute('''INSERT INTO news_sources(name,kind,query,url,topic,latitude,longitude,interval_minutes,enabled,created_at)
                  VALUES('Новости России','search','новости России сегодня','','Город',55.7519,37.6176,15,1,?)''', (stamp(),))

    def acquire(self, key, seconds=90):
        with self.db() as con:
            con.execute('''INSERT INTO worker_locks(key,owner,expires_at) VALUES(?,?,?)
              ON CONFLICT(key) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at
              WHERE worker_locks.expires_at<=? OR worker_locks.owner=excluded.owner''',
                        (key, self.owner, int(time.time())+seconds, int(time.time())))
            return con.execute('SELECT owner FROM worker_locks WHERE key=?', (key,)).fetchone()['owner'] == self.owner

    def release(self, key):
        with self.db() as con:
            con.execute('DELETE FROM worker_locks WHERE key=? AND owner=?', (key, self.owner))

    def routes(self):
        router = APIRouter()
        db, auth, editor = self.db, self.auth, self.editor

        @router.get('/api/admin/users')
        def users(session=Depends(editor)):
            if session['role'] != 'admin':
                raise HTTPException(403, 'Управление доступом только у администратора')
            with db() as con:
                return {'items': [dict(r) for r in con.execute('SELECT id,nickname,role FROM users ORDER BY id DESC LIMIT 300')]}

        @router.patch('/api/admin/users/{user_id}/role')
        def set_role(user_id: int, payload: RoleIn, session=Depends(editor)):
            if session['role'] != 'admin':
                raise HTTPException(403, 'Управление доступом только у администратора')
            with db() as con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute('SELECT role FROM users WHERE id=?', (user_id,)).fetchone()
                if not row:
                    raise HTTPException(404, 'Пользователь не найден')
                if row['role'] == 'admin':
                    raise HTTPException(409, 'Роль администратора нельзя менять через эту панель')
                con.execute('UPDATE users SET role=? WHERE id=?', (payload.role, user_id))
                if payload.role == 'user':
                    con.execute("UPDATE telegram_deliveries SET status='cancelled' WHERE user_id=? AND status='pending'", (user_id,))
                    con.execute('DELETE FROM telegram_link_codes WHERE user_id=?', (user_id,))
            return {'ok': True}

        @router.get('/api/my/telegram')
        def telegram_status(session=Depends(auth)):
            with db() as con:
                row = con.execute('SELECT enabled FROM telegram_links WHERE user_id=?', (session['user_id'],)).fetchone()
                bot = con.execute("SELECT value FROM worker_state WHERE key='telegram-username'").fetchone()
            return {'linked': bool(row), 'enabled': bool(row and row['enabled']),
                    'configured': bool(os.getenv('TELEGRAM_BOT_TOKEN')), 'bot_username': os.getenv('TELEGRAM_BOT_USERNAME') or (bot['value'] if bot else ''),
                    'mini_app_configured': bool(self.app_url())}

        @router.post('/api/my/telegram/link')
        def link(session=Depends(editor)):
            if session['user_id'] is None:
                raise HTTPException(409, 'Войдите в личный кабинет и откройте редакцию оттуда')
            if not os.getenv('TELEGRAM_BOT_TOKEN'):
                raise HTTPException(503, 'Токен бота не настроен на сервере')
            code = secrets.token_urlsafe(24)
            with db() as con:
                con.execute('DELETE FROM telegram_link_codes WHERE expires_at<? OR user_id=?', (int(time.time()), session['user_id']))
                con.execute('INSERT INTO telegram_link_codes VALUES(?,?,?)', (hashlib.sha256(code.encode()).hexdigest(), session['user_id'], int(time.time())+600))
                bot = con.execute("SELECT value FROM worker_state WHERE key='telegram-username'").fetchone()
            username = os.getenv('TELEGRAM_BOT_USERNAME') or (bot['value'] if bot else '')
            return {'code': code, 'expires_in': 600, 'url': f'https://t.me/{username}?start={code}' if re.fullmatch(r'[\w]{5,32}', username) else None}

        @router.delete('/api/my/telegram')
        def unlink(session=Depends(auth)):
            with db() as con:
                con.execute('DELETE FROM telegram_links WHERE user_id=?', (session['user_id'],))
                con.execute('DELETE FROM telegram_link_codes WHERE user_id=?', (session['user_id'],))
                con.execute("UPDATE telegram_deliveries SET status='cancelled' WHERE user_id=? AND status='pending'", (session['user_id'],))
            return {'ok': True}

        @router.post('/api/auth/telegram')
        def telegram_auth(payload: TelegramAuth):
            data = self.verify_init_data(payload.init_data)
            tg_id = data['id']
            with db() as con:
                link = con.execute('SELECT user_id FROM telegram_links WHERE telegram_user_id=?', (tg_id,)).fetchone()
                if link:
                    uid = link['user_id']
                else:
                    phone = f'telegram:{tg_id}'
                    con.execute('INSERT OR IGNORE INTO users(phone,nickname,created_at) VALUES(?,?,?)',
                                (phone, plain(data.get('first_name', 'Telegram'), 32) or 'Telegram', stamp()))
                    uid = con.execute('SELECT id FROM users WHERE phone=?', (phone,)).fetchone()['id']
                role = con.execute('SELECT role FROM users WHERE id=?', (uid,)).fetchone()['role']
                user = self.user_view(con, uid, role)
            return {'token': self.make_session(uid, role), 'user': user}

        @router.get('/api/editor/sources')
        def sources(_=Depends(editor)):
            with db() as con:
                return {'items': [dict(r) for r in con.execute('SELECT * FROM news_sources ORDER BY id')],
                        'worker_enabled': os.getenv('NEWS_WORKER_ENABLED', '1') == '1'}

        @router.post('/api/editor/sources')
        def add_source(payload: SourceIn, _=Depends(editor)):
            with db() as con:
                cur = con.execute('''INSERT INTO news_sources(name,kind,query,url,topic,latitude,longitude,interval_minutes,enabled,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?)''', (*payload.model_dump().values(), stamp()))
                return {'id': cur.lastrowid}

        @router.put('/api/editor/sources/{source_id}')
        def edit_source(source_id: int, payload: SourceIn, _=Depends(editor)):
            with db() as con:
                cur = con.execute('''UPDATE news_sources SET name=?,kind=?,query=?,url=?,topic=?,latitude=?,longitude=?,interval_minutes=?,enabled=?,
                  etag=NULL,modified=NULL,next_check=0 WHERE id=?''', (*payload.model_dump().values(), source_id))
                if not cur.rowcount:
                    raise HTTPException(404, 'Источник не найден')
            return {'ok': True}

        @router.post('/api/editor/sources/collect')
        async def collect(_=Depends(editor)):
            async with httpx.AsyncClient() as client:
                return await self.collect(client, force=True)
        return router

    @staticmethod
    def verify_init_data(raw):
        token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        if not token:
            raise HTTPException(503, 'Telegram не настроен')
        pairs = parse_qsl(raw, keep_blank_values=True)
        fields = dict(pairs)
        signature = fields.pop('hash', '')
        if len(pairs) != len(dict(pairs)):
            raise HTTPException(401, 'Некорректные данные Telegram')
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        try:
            age = time.time() - int(fields['auth_date'])
            user = json.loads(fields['user'])
            valid = isinstance(user.get('id'), int) and user['id'] > 0
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(401, 'Некорректные данные Telegram')
        if not hmac.compare_digest(signature, expected) or not -30 <= age <= 600 or not valid:
            raise HTTPException(401, 'Подпись Telegram неверна или устарела. Откройте приложение снова')
        return user

    def bind_chat(self, code, telegram_id, chat_id):
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.db() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('''SELECT c.user_id FROM telegram_link_codes c JOIN users u ON u.id=c.user_id
              WHERE code_hash=? AND expires_at>? AND u.role IN ('editor','admin')''', (digest, int(time.time()))).fetchone()
            if not row:
                return False
            other = con.execute('SELECT user_id FROM telegram_links WHERE telegram_user_id=? OR chat_id=?', (telegram_id, chat_id)).fetchone()
            if other and other['user_id'] != row['user_id']:
                return False
            con.execute('DELETE FROM telegram_links WHERE user_id=?', (row['user_id'],))
            con.execute('INSERT INTO telegram_links(user_id,telegram_user_id,chat_id) VALUES(?,?,?)', (row['user_id'], telegram_id, chat_id))
            con.execute('DELETE FROM telegram_link_codes WHERE user_id=?', (row['user_id'],))
            # Never reroute older pending notifications to a newly linked chat.
            con.execute("UPDATE telegram_deliveries SET status='cancelled' WHERE user_id=? AND status='pending'", (row['user_id'],))
            return True

    @staticmethod
    def app_url():
        url = os.getenv('NEWSRADAR_PUBLIC_URL', '').rstrip('/')
        return url if web_url(url) and url.startswith('https://') else ''

    def import_entries(self, source, entries):
        count = 0
        with self.db() as con:
            con.execute('BEGIN IMMEDIATE')
            # Source may have been disabled while the HTTP request was running.
            active = con.execute('SELECT enabled FROM news_sources WHERE id=?', (source['id'],)).fetchone()
            if not active or not active['enabled']:
                return 0
            for item in entries[:20]:
                title = plain(item.get('title'), 180)
                link = urljoin(source['url'] or search_url(source['query']), item.get('link', ''))
                if len(title) < 8 or not web_url(link):
                    continue
                # URL is global: overlap between queries/feeds does not produce duplicates.
                key = hashlib.sha256(link.encode()).hexdigest()
                if con.execute('SELECT 1 FROM source_items WHERE dedupe_key=?', (key,)).fetchone():
                    continue
                existing = con.execute('SELECT id FROM events WHERE source_url=? LIMIT 1', (link,)).fetchone()
                if existing:
                    event_id = existing['id']
                else:
                    cur = con.execute('''INSERT INTO events(title,summary,topic,latitude,longitude,status,verification,source_url,created_at,updated_at)
                      VALUES(?,?,?,?,?,'review','unverified',?,?,?)''',
                                      (title, plain(item.get('summary') or item.get('description') or title), source['topic'], source['latitude'], source['longitude'], link, stamp(), stamp()))
                    event_id = cur.lastrowid
                    con.execute('UPDATE events SET location_known=0 WHERE id=?', (event_id,))
                    count += 1
                con.execute('INSERT INTO source_items VALUES(?,?,?,?)', (key, source['id'], event_id, stamp()))
            con.execute('UPDATE news_sources SET imported_count=imported_count+? WHERE id=?', (count, source['id']))
        return count

    async def collect(self, client, force=False):
        if self.collect_lock.locked():
            return {'imported': 0, 'errors': [], 'busy': True}
        async with self.collect_lock:
            with self.db() as con:
                sources = [dict(r) for r in con.execute('SELECT * FROM news_sources WHERE enabled=1 AND (? OR next_check<=?)', (int(force), int(time.time())))]
            count, errors = 0, []
            for source in sources:
                key = f"source:{source['id']}"
                if not self.acquire(key):
                    continue
                try:
                    headers = {'User-Agent': 'NewsRadar/0.2 RSS reader'}
                    if source['etag']:
                        headers['If-None-Match'] = source['etag']
                    if source['modified']:
                        headers['If-Modified-Since'] = source['modified']
                    entries, metadata = await fetch_feed(client, source['url'] if source['kind'] == 'rss' else search_url(source['query']), headers)
                    if entries is not None:
                        count += self.import_entries(source, entries)
                    with self.db() as con:
                        con.execute('''UPDATE news_sources SET last_checked=?,next_check=?,last_error=NULL,
                          etag=COALESCE(?,etag),modified=COALESCE(?,modified) WHERE id=?''',
                                    (stamp(), int(time.time())+source['interval_minutes']*60, metadata.get('etag'), metadata.get('last-modified'), source['id']))
                except Exception as exc:
                    # Do not persist arbitrary URLs, credentials or response bodies in errors.
                    error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    errors.append({'source_id': source['id'], 'error': error})
                    with self.db() as con:
                        con.execute('UPDATE news_sources SET last_checked=?,next_check=?,last_error=? WHERE id=?',
                                    (stamp(), int(time.time())+300, error[:200], source['id']))
                finally:
                    self.release(key)
            return {'imported': count, 'errors': errors}

    async def run(self):
        from app.bots.telegram_bot import TelegramBot
        async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=10)) as client:
            bot = TelegramBot(self, client)
            tasks = [asyncio.create_task(self.discovery_loop(client))]
            if os.getenv('TELEGRAM_BOT_TOKEN'):
                tasks += [asyncio.create_task(bot.poll()), asyncio.create_task(bot.delivery_loop())]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                with self.db() as con:
                    con.execute('DELETE FROM worker_locks WHERE owner=?', (self.owner,))

    async def discovery_loop(self, client):
        while True:
            try:
                await self.collect(client)
            except Exception as exc:
                log.warning('Discovery loop: %s', type(exc).__name__)
            await asyncio.sleep(30)

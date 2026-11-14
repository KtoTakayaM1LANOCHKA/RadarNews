"""Telegram intake + Mini App + editor notifications. Runs with the web server."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time

import httpx
from app.newsroom import stamp

log = logging.getLogger(__name__)
MAX_BYTES = 12 * 1024 * 1024


class TelegramError(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(f'Telegram API error {code}')


class TelegramBot:
    def __init__(self, newsroom, client):
        self.newsroom, self.client = newsroom, client
        self.token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self.base = f'https://api.telegram.org/bot{self.token}/'

    async def tg(self, method, **fields):
        try:
            response = await self.client.post(self.base + method, json=fields)
            data = response.json()
        except Exception as exc:
            # httpx exception strings contain the URL with the bot token.
            raise TelegramError(502) from None
        if not data.get('ok'):
            raise TelegramError(data.get('error_code', response.status_code), (data.get('parameters') or {}).get('retry_after', 0))
        return data['result']

    def app_markup(self):
        url = self.newsroom.app_url()
        return {'inline_keyboard': [[{'text': 'Открыть NewsRadar', 'web_app': {'url': url}}]]} if url else None

    async def send(self, chat_id, text, markup=None):
        fields = {'chat_id': chat_id, 'text': text[:4000]}
        if markup:
            fields['reply_markup'] = markup
        return await self.tg('sendMessage', **fields)

    def editor_account(self, telegram_id):
        with self.newsroom.db() as con:
            return con.execute('''SELECT u.id,u.role FROM telegram_links l JOIN users u ON u.id=l.user_id
              WHERE l.telegram_user_id=? AND u.role IN ('editor','admin')''', (telegram_id,)).fetchone()

    async def upload(self, submission_id, media):
        from app.main import store_media_data
        if int(media.get('file_size', 0)) > MAX_BYTES:
            raise ValueError('Размер медиа превышает 12 МБ')
        info = await self.tg('getFile', file_id=media['file_id'])
        if int(info.get('file_size', 0)) > MAX_BYTES:
            raise ValueError('Размер медиа превышает 12 МБ')
        path = info['file_path']
        # Only paths returned by Telegram, without an authority or traversal.
        if not re.fullmatch(r'[\w./-]+', path) or '..' in path.split('/') or path.startswith('/'):
            raise ValueError('Неверный путь медиа')
        ext = path.lower().split('.')[-1]
        mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'mp4': 'video/mp4', 'webm': 'video/webm'}.get(ext)
        if not mime:
            raise ValueError('Неподдерживаемый формат')
        body = bytearray()
        async with self.client.stream('GET', f'https://api.telegram.org/file/bot{self.token}/{path}', timeout=30) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ValueError('Размер медиа превышает 12 МБ')
        store_media_data(submission_id, bytes(body), mime, 'telegram.' + ext)

    async def handle(self, update):
        from app.main import BotIn, create_bot_submission
        msg = update.get('message') or {}
        # Editorial access and account linking are restricted to personal chats.
        if not msg or msg.get('chat', {}).get('type') != 'private' or not msg.get('from'):
            return
        chat, uid = msg['chat']['id'], msg['from']['id']
        text = (msg.get('text') or msg.get('caption') or '').strip()
        command = text.split(maxsplit=1)[0].split('@')[0].lower() if text else ''
        if command == '/start' and len(text.split()) == 2:
            ok = self.newsroom.bind_chat(text.split()[1], uid, chat)
            await self.send(chat, 'Редакторский аккаунт привязан. Новые новости и ответы пользователей придут в этот чат. /stop — отключить уведомления.' if ok else 'Код недействителен, истёк или Telegram уже связан с другим аккаунтом. Получите новый код в личном кабинете.', self.app_markup())
            return
        if command in ('/start', '/help', '/app'):
            await self.send(chat, 'NewsRadar: карта, новости, задачи, репутация и кабинет доступны в приложении. Пришлите текст, фото или видео — передам редактору. /nearby — задачи; /search запрос — поиск для редакторов; /stop и /subscribe — уведомления. Для редакторских прав сначала привяжите Telegram в личном кабинете.' + ('' if self.newsroom.app_url() else '\nКнопка приложения появится после настройки HTTPS-адреса на сервере.'), self.app_markup())
            return
        if command in ('/stop', '/subscribe'):
            if not self.editor_account(uid):
                await self.send(chat, 'Для рассылки сначала привяжите редакторский аккаунт в личном кабинете.')
                return
            with self.newsroom.db() as con:
                con.execute('UPDATE telegram_links SET enabled=? WHERE telegram_user_id=?', (int(command == '/subscribe'), uid))
                if command == '/stop':
                    con.execute("UPDATE telegram_deliveries SET status='cancelled' WHERE chat_id=? AND status='pending'", (chat,))
            await self.send(chat, 'Уведомления отключены.' if command == '/stop' else 'Уведомления включены. Будут приходить новые материалы.')
            return
        if command == '/nearby':
            with self.newsroom.db() as con:
                items = con.execute("SELECT id,title FROM events WHERE status='order' ORDER BY id DESC LIMIT 8").fetchall()
            await self.send(chat, 'Открытые задачи (без учёта местоположения):\n' + ('\n'.join(f"#{x['id']}: {x['title']}" for x in items) or 'Задач нет'), self.app_markup())
            return
        if command == '/search':
            if not self.editor_account(uid):
                await self.send(chat, 'Поиск доступен привязанным редакторам.')
                return
            query = text.partition(' ')[2].strip()
            if not 3 <= len(query) <= 180:
                await self.send(chat, 'Используйте /search запрос (3–180 символов).')
                return
            from app.newsroom import fetch_feed, plain, search_url, web_url
            entries, _ = await fetch_feed(self.client, search_url(query))
            result = '\n\n'.join(f"{plain(e.get('title'), 180)}\n{e.get('link', '')}" for e in (entries or [])[:5] if web_url(e.get('link', '')))
            await self.send(chat, 'Результаты поиска — требуют проверки:\n\n' + (result or 'Ничего не найдено'), self.app_markup())
            return
        if command.startswith('/'):
            await self.send(chat, 'Неизвестная команда. /help — список команд.')
            return
        media = (msg.get('photo') or [None])[-1] or msg.get('video') or msg.get('document')
        if not text and not media:
            await self.send(chat, 'Пришлите описание события, фото или видео. Геолокацию можно приложить в Mini App.')
            return
        if len(text) > 3000:
            await self.send(chat, 'Описание должно содержать не более 3000 символов.')
            return
        if not media and len(text) < 5:
            await self.send(chat, 'Опишите событие подробнее (от 5 символов).')
            return
        description = text if len(text) >= 5 else (text + ' · Медиафайл для проверки' if text else 'Медиа без описания — нужно уточнение')
        created = create_bot_submission(BotIn(provider='telegram', provider_user_id=str(uid), text=description, message_id=msg['message_id'], chat_id=chat))
        # Link intake to the account used by the Mini App, so rewards/history work there too.
        with self.newsroom.db() as con:
            account = con.execute('SELECT user_id AS id FROM telegram_links WHERE telegram_user_id=?', (uid,)).fetchone()
            if not account:
                con.execute('INSERT OR IGNORE INTO users(phone,nickname,created_at) VALUES(?,?,?)', (f'telegram:{uid}', str(msg['from'].get('first_name', 'Telegram'))[:32], stamp()))
                account = con.execute('SELECT id FROM users WHERE phone=?', (f'telegram:{uid}',)).fetchone()
            con.execute('UPDATE submissions SET user_id=? WHERE id=?', (account['id'], created['submission_id']))
            done = con.execute('SELECT media_done FROM telegram_messages WHERE chat_id=? AND message_id=?', (chat, msg['message_id'])).fetchone()['media_done']
        if media and not done:
            try:
                await self.upload(created['submission_id'], media)
                with self.newsroom.db() as con:
                    con.execute('UPDATE telegram_messages SET media_done=1 WHERE chat_id=? AND message_id=?', (chat, msg['message_id']))
            except Exception as exc:
                log.warning('Telegram upload: %s', type(exc).__name__)
                await self.send(chat, f"Текст новости #{created['event_id']} сохранён, но медиа загрузить не удалось. Допустимы JPG/PNG/WebP/MP4/WebM до 12 МБ. Добавьте файл через приложение.", self.app_markup())
                return
        await self.send(chat, f"Сообщение #{created['event_id']} в очереди редактора. Публикация после проверки.", self.app_markup())

    async def poll(self):
        configured = False
        while True:
            if not self.newsroom.acquire('telegram-poll', 180):
                await asyncio.sleep(5)
                continue
            try:
                if not configured:
                    info = await self.tg('getMe')
                    os.environ['TELEGRAM_BOT_USERNAME'] = info['username']
                    with self.newsroom.db() as con:
                        con.execute("INSERT OR REPLACE INTO worker_state VALUES('telegram-username',?)", (info['username'],))
                    await self.tg('setMyCommands', commands=[{'command': c, 'description': d} for c, d in (
                        ('app', 'Открыть приложение'), ('help', 'Помощь'), ('nearby', 'Задачи на подтверждение'),
                        ('search', 'Поиск новостей для редакторов'), ('stop', 'Отключить уведомления'), ('subscribe', 'Включить уведомления'))])
                    if self.newsroom.app_url():
                        await self.tg('setChatMenuButton', menu_button={'type': 'web_app', 'text': 'NewsRadar', 'web_app': {'url': self.newsroom.app_url()}})
                    configured = True
                with self.newsroom.db() as con:
                    row = con.execute("SELECT value FROM worker_state WHERE key='telegram-offset'").fetchone()
                updates = await self.tg('getUpdates', offset=int(row['value']) if row else None, timeout=25, limit=10, allowed_updates=['message'])
                for update in updates:
                    self.newsroom.acquire('telegram-poll', 180)
                    try:
                        await self.handle(update)
                    except TelegramError:
                        raise  # Retry the update on API/network failure.
                    except Exception as exc:
                        log.warning('Telegram message: %s', type(exc).__name__)
                        msg = update.get('message') or {}
                        if msg.get('chat', {}).get('type') == 'private':
                            await self.send(msg['chat']['id'], 'Не удалось выполнить действие. Повторите позже или откройте приложение.', self.app_markup())
                    with self.newsroom.db() as con:
                        con.execute("INSERT OR REPLACE INTO worker_state VALUES('telegram-offset',?)", (str(update['update_id']+1),))
            except Exception as exc:
                log.warning('Telegram polling: %s', type(exc).__name__)
                await asyncio.sleep(5)

    async def deliver(self):
        with self.newsroom.db() as con:
            pending = [dict(r) for r in con.execute("SELECT id FROM telegram_deliveries WHERE status='pending' AND next_try<=? ORDER BY id LIMIT 30", (int(time.time()),))]
        for job in pending:
            key = f"delivery:{job['id']}"
            if not self.newsroom.acquire(key, 60):
                continue
            try:
                with self.newsroom.db() as con:
                    row = con.execute('''SELECT d.*,e.title,e.summary,e.source_url,u.role,l.enabled,l.chat_id AS current_chat
                      FROM telegram_deliveries d JOIN events e ON e.id=d.event_id JOIN users u ON u.id=d.user_id
                      LEFT JOIN telegram_links l ON l.user_id=d.user_id WHERE d.id=?''', (job['id'],)).fetchone()
                    if not row or row['status'] != 'pending':
                        continue
                    if row['role'] not in ('editor', 'admin') or not row['enabled'] or row['current_chat'] != row['chat_id']:
                        con.execute("UPDATE telegram_deliveries SET status='cancelled' WHERE id=?", (job['id'],))
                        continue
                    row = dict(row)
                url = self.newsroom.app_url()
                # Web App opens the existing app; editor route is a normal HTTPS link.
                markup = {'inline_keyboard': [[{'text': 'Открыть в редакции', 'web_app': {'url': url + f"/?editor_event={row['event_id']}"}}]]} if url else None
                heading = 'Новый ответ по новости' if row['kind'] == 'evidence' else 'Новая новость · нужна проверка'
                text = f"{heading}\n#{row['event_id']}: {row['title']}\n\n{row['summary'][:1800]}"
                if row['source_url']:
                    text += '\n\nИсточник: ' + row['source_url'][:900]
                try:
                    await self.send(row['chat_id'], text, markup)
                except TelegramError as exc:
                    with self.newsroom.db() as con:
                        if exc.code == 403:
                            con.execute('UPDATE telegram_links SET enabled=0 WHERE user_id=?', (row['user_id'],))
                        attempts = row['attempts'] + 1
                        con.execute('''UPDATE telegram_deliveries SET attempts=?,next_try=?,last_error=?,status=? WHERE id=?''',
                                    (attempts, int(time.time())+max(exc.retry_after, min(3600, 2**min(attempts, 10)*5)), str(exc), 'failed' if exc.code in (400, 403) else 'pending', row['id']))
                else:
                    with self.newsroom.db() as con:
                        con.execute("UPDATE telegram_deliveries SET status='sent',last_error=NULL WHERE id=?", (row['id'],))
                    await asyncio.sleep(.05)
            finally:
                self.newsroom.release(key)

    async def delivery_loop(self):
        while True:
            try:
                await self.deliver()
            except Exception as exc:
                log.warning('Telegram deliveries: %s', type(exc).__name__)
            await asyncio.sleep(3)


async def run():
    # Alternative standalone worker; set NEWS_WORKER_ENABLED=0 on the web process.
    from app.main import newsroom
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        raise SystemExit('Set TELEGRAM_BOT_TOKEN first')
    await newsroom.run()


if __name__ == '__main__':
    asyncio.run(run())

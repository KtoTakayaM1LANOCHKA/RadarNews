"""NewsRadar MVP: runnable demonstration, NOT an automated truth-verification service.

Backend: FastAPI + SQLite. Built-in demo data, editorial review, local orders,
user submissions, cautious image metadata inspection and optional public search.
No automated publishing; real SMS and most bot connectors require credentials.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import time
import asyncio
from contextlib import contextmanager, asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from PIL import Image, ImageOps, UnidentifiedImageError
from app.ai_optional import llm_triage
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env', override=False)
STATIC = ROOT / 'app' / 'static'
# В Amvera Cloud (переменная AMVERA=1) данные лежат в постоянном хранилище /data.
DATA_DIR = Path('/data') if os.getenv('AMVERA') else ROOT
DB_FILE = Path(os.getenv('DB_PATH', str(DATA_DIR / 'newsradar.db'))).resolve()
UPLOAD_DIR = Path(os.getenv('UPLOAD_DIR', str(DATA_DIR / 'uploads'))).resolve()
AVATAR_DIR = UPLOAD_DIR / 'avatars'
DEMO_MODE = os.getenv('DEMO_MODE', '1') == '1'
DEMO_OTP = os.getenv('DEMO_OTP', '1') == '1' and DEMO_MODE
EDITOR_PASSWORD = os.getenv('EDITOR_PASSWORD', 'change-this-before-deployment')
# Вход по этому номеру открывает учётную запись администратора (пользователь + редактор).
ADMIN_PHONE = re.sub(r'[\s()\-]', '', os.getenv('ADMIN_PHONE', '+79999999999'))
MAX_BYTES = 12 * 1024 * 1024
SESSION_DAYS = 30

# Баллы репутации и вознаграждения (демо-тарифы). Начисляются на следующий день после решения редактора.
CONFIRM_POINTS = 10        # принятое подтверждение или опровержение по задаче
CONFIRM_PENALTY = -5       # отклонённое подтверждение или опровержение
FALSE_NEWS_PENALTY = -20   # недостоверная (отклонённая) новость, без денег
TIERS = [(80, 'Эксклюзив', 1500), (60, 'Значимая', 500), (0, 'Обычная', 150)]  # (баллы от, тип новости, ₽)
MIN_WITHDRAW = 100
MSK = timezone(timedelta(hours=3))
DB_FILE.parent.mkdir(parents=True, exist_ok=True)
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(newsroom.run()) if os.getenv('NEWS_WORKER_ENABLED', '1') == '1' else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title='NewsRadar MVP', version='0.2.1', lifespan=lifespan, docs_url='/api/docs', openapi_url='/api/openapi.json')
app.add_middleware(GZipMiddleware, minimum_size=1024)


@contextmanager
def db():
    con = sqlite3.connect(DB_FILE, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA journal_mode=WAL')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def trim(s, maximum=1000):
    return (s or '').strip()[:maximum]


def rows_to_list(cursor):
    return [dict(x) for x in cursor.fetchall()]


def news_reward(importance, exclusive):
    """Баллы за достоверную новость: 10 + 0,45·значимость + 0,45·эксклюзивность (10–100); деньги — по типу."""
    points = 10 + (45 * (importance + exclusive) + 50) // 100
    name, money = next((n, m) for start, n, m in TIERS if points >= start)
    return points, money, name


def next_day_ts():
    tomorrow = datetime.now(MSK) + timedelta(days=1)
    return int(tomorrow.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def set_reward(con, sub, points, money, reason):
    """Итог по сообщению должен стать (points, money): неначисленное отменяется, начисленное правится разницей."""
    con.execute('DELETE FROM rewards WHERE submission_id=? AND applied=0', (sub['id'],))
    done = con.execute('SELECT COALESCE(SUM(points),0) AS p, COALESCE(SUM(money),0) AS m FROM rewards WHERE submission_id=?', (sub['id'],)).fetchone()
    dp, dm = points - done['p'], money - done['m']
    if dp or dm:
        con.execute('INSERT INTO rewards(user_id,submission_id,points,money,reason,created_at,available_at) VALUES(?,?,?,?,?,?,?)',
                    (sub['user_id'], sub['id'], dp, dm, reason, now(), next_day_ts()))


def settle(con, user_id):
    """Зачисляет баллы и деньги, срок которых наступил."""
    t = int(time.time())
    due = con.execute('SELECT COALESCE(SUM(points),0) AS p, COALESCE(SUM(money),0) AS m FROM rewards WHERE user_id=? AND applied=0 AND available_at<=?', (user_id, t)).fetchone()
    if due['p'] or due['m']:
        con.execute('UPDATE users SET credits=credits+?, wallet=MAX(0,wallet+?) WHERE id=?', (due['p'], due['m'], user_id))
    con.execute('UPDATE rewards SET applied=1 WHERE user_id=? AND applied=0 AND available_at<=?', (user_id, t))


def user_view(con, user_id, role='user'):
    settle(con, user_id)
    u = con.execute('SELECT id,phone,nickname,avatar,credits,wallet FROM users WHERE id=?', (user_id,)).fetchone()
    if not u:
        raise HTTPException(401, 'Аккаунт не найден')
    pending = con.execute('SELECT COALESCE(SUM(points),0) AS p, COALESCE(SUM(money),0) AS m FROM rewards WHERE user_id=? AND applied=0', (user_id,)).fetchone()
    return {**dict(u), 'role': role,
            'nickname': u['nickname'] or ('Админ' if role == 'admin' else f"Очевидец #{u['id']}"),
            'avatar': f"/api/avatars/{u['avatar']}" if u['avatar'] else None,
            'pending_points': pending['p'], 'pending_money': pending['m']}


def status_description(value):
    return {'review':'На проверке редактора', 'order':'Нужны подтверждения', 'published':'Опубликовано (демо)', 'rejected':'Отклонено'}.get(value, value)


def init_db():
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT UNIQUE NOT NULL,
          created_at TEXT NOT NULL, credits INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS otps (
          phone TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at INTEGER NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, issued_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS otp_issues (
          id INTEGER PRIMARY KEY, phone TEXT NOT NULL, issued_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER,
          role TEXT NOT NULL, expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, summary TEXT NOT NULL,
          topic TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
          status TEXT NOT NULL DEFAULT 'review', verification TEXT NOT NULL DEFAULT 'unverified',
          importance INTEGER NOT NULL DEFAULT 50, exclusive_score INTEGER NOT NULL DEFAULT 50,
          popularity INTEGER NOT NULL DEFAULT 0, promoted INTEGER NOT NULL DEFAULT 0,
          ad_label TEXT, source_url TEXT, demo INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES events(id),
          question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
          reward_points INTEGER NOT NULL DEFAULT 30, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS submissions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id INTEGER NOT NULL REFERENCES events(id), user_id INTEGER,
          provider TEXT NOT NULL DEFAULT 'web', provider_user_id TEXT,
          kind TEXT NOT NULL, text TEXT NOT NULL,
          latitude REAL, longitude REAL, contact_opt_in INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending',
          submitted_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS media (
          id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL REFERENCES submissions(id),
          stored_name TEXT NOT NULL, original_name TEXT NOT NULL,
          mime TEXT NOT NULL, byte_size INTEGER NOT NULL,
          exif_summary TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES events(id),
          action TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS drafts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES events(id),
          version INTEGER NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS analyses (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES events(id),
          summary TEXT NOT NULL, flags TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rewards (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
          submission_id INTEGER REFERENCES submissions(id), points INTEGER NOT NULL,
          money INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL, created_at TEXT NOT NULL,
          available_at INTEGER NOT NULL, applied INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
          amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'requested', created_at TEXT NOT NULL
        );
        ''')
        have = {r['name'] for r in con.execute('PRAGMA table_info(users)')}
        for column, ddl in (('nickname', 'TEXT'), ('avatar', 'TEXT'), ('wallet', 'INTEGER NOT NULL DEFAULT 0'), ('role', "TEXT NOT NULL DEFAULT 'user'")):
            if column not in have:
                con.execute(f'ALTER TABLE users ADD COLUMN {column} {ddl}')
        con.execute("UPDATE users SET role='admin' WHERE phone=?", (ADMIN_PHONE,))
        if 'location_known' not in {r['name'] for r in con.execute('PRAGMA table_info(events)')}:
            con.execute('ALTER TABLE events ADD COLUMN location_known INTEGER NOT NULL DEFAULT 1')
        con.execute('UPDATE orders SET reward_points=?', (CONFIRM_POINTS,))
        if con.execute('SELECT count(*) AS n FROM events').fetchone()['n'] == 0:
            seeds = [
                ('Возможно изменение маршрута автобуса №7','В локальном обсуждении сообщили об изменении маршрута. Нужна ссылка на расписание или сообщение перевозчика.','Транспорт',55.7519,37.6186,'order','unverified',69,74,37,0,None),
                ('Открытие выставки современной графики','Организаторы объявили о выставке в центре города. Показаны демонстрационные сведения.','Культура',55.7591,37.6062,'published','verified',65,41,100,0,None),
                ('Фестиваль музыки в городском парке','Демо-событие с программой выступлений и афишей.','Афиша',55.7448,37.6319,'published','verified',72,50,270,0,None),
                ('Концерт — партнёрская афиша','Демонстрационное рекламное мероприятие с интерактивной карточкой.','Афиша',55.7542,37.6369,'published','verified',40,24,30,1,'Реклама'),
                ('Уточняется режим работы спортивного комплекса','Есть сообщение о временном изменении режима работы. Официальных данных пока недостаточно.','Город',55.7611,37.6402,'order','unverified',57,78,15,0,None),
                ('Новая площадка для настольных игр','Инициативная группа анонсировала открытую встречу.','Досуг',55.7503,37.5913,'review','unverified',54,62,12,0,None),
            ]
            for title,summary,topic,lat,lon,status,verification,importance,exclusive,pop,promoted,label in seeds:
                cur = con.execute('''INSERT INTO events(title,summary,topic,latitude,longitude,status,verification,importance,exclusive_score,popularity,promoted,ad_label,demo,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)''', (title,summary,topic,lat,lon,status,verification,importance,exclusive,pop,promoted,label,now(),now()))
                if status == 'order':
                    question = 'Нужны уже имеющиеся фото, документы или ссылки на официальные сведения. Можно подтвердить или опровергнуть.'
                    con.execute('INSERT INTO orders(event_id,question,reward_points,created_at) VALUES (?,?,?,?)',(cur.lastrowid,question,CONFIRM_POINTS,now()))


init_db()


def result_event(con, e):
    d = dict(e)
    d['status_label'] = status_description(d['status'])
    d['order'] = con.execute("SELECT id,question,status,reward_points FROM orders WHERE event_id=? AND status='open' ORDER BY id DESC LIMIT 1",(d['id'],)).fetchone()
    d['order'] = dict(d['order']) if d['order'] else None
    d['submission_count'] = con.execute('SELECT count(*) AS n FROM submissions WHERE event_id=?',(d['id'],)).fetchone()['n']
    return d


def distance_km(lat1,lon1,lat2,lon2):
    r=6371
    p1,p2=math.radians(lat1),math.radians(lat2)
    dp=math.radians(lat2-lat1)
    dl=math.radians(lon2-lon1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(min(1,math.sqrt(a)))


class PhoneIn(BaseModel):
    phone: str

class CodeIn(PhoneIn):
    code: str

class NewSubmission(BaseModel):
    event_id: int|None = None
    kind: Literal['tip','confirm','refute'] = 'tip'
    text: str = Field(min_length=8,max_length=3000)
    latitude: float|None = Field(None,ge=-90,le=90)
    longitude: float|None = Field(None,ge=-180,le=180)
    contact_opt_in: bool = False
    title: str|None = None
    topic: str = 'Город'

class EditorKey(BaseModel):
    password: str

class ProfileIn(BaseModel):
    nickname: str = Field(min_length=2,max_length=32)

class DecisionIn(BaseModel):
    action: Literal['reject','publish','revise']
    note: str = Field(default='',max_length=1600)
    question: str = Field(default='',max_length=1200)
    importance: int|None = Field(None,ge=0,le=100)
    exclusive_score: int|None = Field(None,ge=0,le=100)
    latitude: float|None = Field(None,ge=-90,le=90)
    longitude: float|None = Field(None,ge=-180,le=180)

class ReviewSubmission(BaseModel):
    accepted: bool

class DiscoveryIn(BaseModel):
    query: str = Field(min_length=3,max_length=180)


def phone_clean(phone):
    s = re.sub(r'[\s()\-]', '',phone)
    if not re.fullmatch(r'\+[1-9][0-9]{9,14}',s):
        raise HTTPException(422,'Введите телефон в международном формате, например +79991234567')
    return s


def secret_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def make_session(user_id=None,role='user'):
    token=secrets.token_urlsafe(32)
    with db() as con:
        con.execute('INSERT INTO sessions(token_hash,user_id,role,expires_at) VALUES(?,?,?,?)',
                    (secret_hash(token),user_id,role,int(time.time())+SESSION_DAYS*86400))
    return token


def auth(x_session: Annotated[str|None, Header(alias='X-Session')]=None):
    if not x_session:
        raise HTTPException(401,'Требуется вход')
    with db() as con:
        row=con.execute('SELECT user_id,role,expires_at FROM sessions WHERE token_hash=?',(secret_hash(x_session),)).fetchone()
        if not row or row['expires_at'] < int(time.time()):
            raise HTTPException(401,'Сессия истекла')
        result = dict(row)
        if row['user_id'] is not None:
            user = con.execute('SELECT role FROM users WHERE id=?', (row['user_id'],)).fetchone()
            if not user:
                raise HTTPException(401, 'Аккаунт не найден')
            result['role'] = user['role']
        return result


def editor(session=Depends(auth)):
    if session['role'] not in ('editor','admin'):
        raise HTTPException(403,'Доступ только редактору')
    return session


def member(session=Depends(auth)):
    if session['user_id'] is None:
        raise HTTPException(403,'Требуется аккаунт пользователя')
    return session


@app.get('/api/config')
def config():
    return {'demo_mode':DEMO_MODE,'otp_demo':DEMO_OTP,'map_center':[55.7519,37.6176], 'project':'NewsRadar','shorts_enabled':False, 'map_provider':'osm_leaflet',
            'rewards':{'confirm':CONFIRM_POINTS,'confirm_rejected':CONFIRM_PENALTY,'false_news':FALSE_NEWS_PENALTY,'min_withdraw':MIN_WITHDRAW,
                       'tiers':[{'from':start,'name':name,'money':money} for start,name,money in TIERS]}}


@app.post('/api/auth/request-code')
def request_code(payload: PhoneIn, request: Request):
    phone=phone_clean(payload.phone)
    t=int(time.time())
    with db() as con:
        con.execute('DELETE FROM otp_issues WHERE issued_at<?',(t-3600,))
        n=con.execute('SELECT count(*) AS n FROM otp_issues WHERE phone=?',(phone,)).fetchone()['n']
        if n>=5:
            raise HTTPException(429,'Слишком много попыток. Повторите позже')
        existing=con.execute('SELECT issued_at FROM otps WHERE phone=?',(phone,)).fetchone()
        if existing and t-existing['issued_at']<30:
            raise HTTPException(429,'Новый код можно запросить через 30 секунд')
        code=str(secrets.randbelow(10**6)).zfill(6)
        con.execute('INSERT OR REPLACE INTO otps(phone,code_hash,expires_at,attempts,issued_at) VALUES(?,?,?,0,?)',
                    (phone,secret_hash(phone+':'+code),t+300,t))
        con.execute('INSERT INTO otp_issues(phone,issued_at) VALUES(?,?)',(phone,t))
    if DEMO_OTP:
        print(f'[DEMO ONLY] Login verification for {phone}: {code}', flush=True)
        return {'ok':True,'demo_code':code,'message':'Демо: код показан только для локального тестирования'}
    # Production SMS sender intentionally not implemented: avoid fake confirmations.
    with db() as con:
        con.execute('DELETE FROM otps WHERE phone=?',(phone,))
    raise HTTPException(503,'SMS-провайдер не подключён; авторизация недоступна')


@app.post('/api/auth/verify-code')
def verify_code(payload: CodeIn):
    phone=phone_clean(payload.phone)
    if not re.fullmatch(r'[0-9]{6}',payload.code):
        raise HTTPException(422,'Код должен состоять из 6 цифр')
    with db() as con:
        row=con.execute('SELECT * FROM otps WHERE phone=?',(phone,)).fetchone()
        if not row or row['expires_at']<time.time() or row['attempts']>=5:
            raise HTTPException(400,'Неверный или просроченный код')
        con.execute('UPDATE otps SET attempts=attempts+1 WHERE phone=?',(phone,))
        if not hmac.compare_digest(row['code_hash'],secret_hash(phone+':'+payload.code)):
            raise HTTPException(400,'Неверный или просроченный код')
        con.execute('DELETE FROM otps WHERE phone=?',(phone,))
        con.execute('INSERT OR IGNORE INTO users(phone,created_at) VALUES(?,?)',(phone,now()))
        if phone==ADMIN_PHONE:
            con.execute("UPDATE users SET role='admin' WHERE phone=?", (phone,))
        role=con.execute('SELECT role FROM users WHERE phone=?',(phone,)).fetchone()['role']
        user=user_view(con,con.execute('SELECT id FROM users WHERE phone=?',(phone,)).fetchone()['id'],role)
    return {'token':make_session(user['id'],role),'user':user}


@app.post('/api/editor/login')
def login_editor(payload: EditorKey, x_session: Annotated[str|None, Header(alias='X-Session')]=None):
    if not DEMO_MODE:
        raise HTTPException(503,'Редакторская авторизация для production должна быть подключена отдельно')
    if not hmac.compare_digest(payload.password,EDITOR_PASSWORD):
        raise HTTPException(401,'Неверный пароль редактора')
    if x_session:
        session = auth(x_session)
        if session['user_id'] is not None:
            with db() as con:
                con.execute("UPDATE users SET role='editor' WHERE id=? AND role!='admin'", (session['user_id'],))
            return {'token':x_session,'role':auth(x_session)['role']}
    return {'token':make_session(role='editor'),'role':'editor'}


@app.get('/api/auth/me')
def me(session=Depends(auth)):
    if session['user_id'] is None:
        return {'role':'editor'}
    with db() as con:
        return user_view(con,session['user_id'],session['role'])


@app.patch('/api/my/profile')
def update_profile(payload: ProfileIn, session=Depends(member)):
    nickname=re.sub(r'\s+',' ',payload.nickname).strip()
    if len(nickname)<2: raise HTTPException(422,'Ник — от 2 до 32 символов')
    with db() as con:
        con.execute('UPDATE users SET nickname=? WHERE id=?',(nickname,session['user_id']))
        return user_view(con,session['user_id'],session['role'])


@app.post('/api/my/avatar')
async def upload_avatar(file: UploadFile=File(...), session=Depends(member)):
    data=await file.read(MAX_BYTES+1)
    if not data or len(data)>MAX_BYTES: raise HTTPException(413,'Пустой файл или больше 12 МБ')
    try:
        image=ImageOps.fit(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB'),(256,256))
    except (UnidentifiedImageError,OSError,ValueError):
        raise HTTPException(415,'Нужна картинка JPG, PNG или WebP')
    name=secrets.token_hex(16)+'.jpg'
    image.save(AVATAR_DIR/name,'JPEG',quality=85)
    with db() as con:
        old=con.execute('SELECT avatar FROM users WHERE id=?',(session['user_id'],)).fetchone()['avatar']
        con.execute('UPDATE users SET avatar=? WHERE id=?',(name,session['user_id']))
        if old: (AVATAR_DIR/old).unlink(missing_ok=True)
        return user_view(con,session['user_id'],session['role'])


@app.get('/api/avatars/{name}')
def avatar(name:str):
    if not re.fullmatch(r'[0-9a-f]{32}\.jpg',name) or not (AVATAR_DIR/name).exists():
        raise HTTPException(404,'Аватар не найден')
    return FileResponse(AVATAR_DIR/name,media_type='image/jpeg',headers={'Cache-Control':'public, max-age=31536000, immutable'})


@app.post('/api/my/withdraw')
def withdraw(session=Depends(member)):
    """Заявка на вывод всего кошелька. Демо: реальные выплаты не подключены."""
    with db() as con:
        amount=user_view(con,session['user_id'],session['role'])['wallet']
        if amount<MIN_WITHDRAW: raise HTTPException(409,f'Вывод доступен от {MIN_WITHDRAW} ₽')
        con.execute('INSERT INTO withdrawals(user_id,amount,created_at) VALUES(?,?,?)',(session['user_id'],amount,now()))
        con.execute('UPDATE users SET wallet=0 WHERE id=?',(session['user_id'],))
    return {'amount':amount,'status':'requested','message':f'Заявка на вывод {amount} ₽ создана. Демо: реальные выплаты не подключены'}


@app.get('/api/events')
def events(status: str|None=None, topic: str|None=None, lat: float|None=None,lon: float|None=None,radius: float=25):
    if radius<=0 or radius>200: raise HTTPException(422,'Радиус должен быть 0–200 км')
    if (lat is None)!=(lon is None): raise HTTPException(422,'Нужны и широта, и долгота')
    with db() as con:
        sql='SELECT * FROM events WHERE 1=1'
        args=[]
        # Public API never returns unpublished editor-queue or rejected items.
        sql+=" AND status IN ('published','order')"
        if status:
            sql+=' AND status=?'; args.append(status)
        if topic:
            sql+=' AND topic=?'; args.append(topic)
        sql+=' ORDER BY updated_at DESC, id DESC LIMIT 300'
        out=[]
        for row in con.execute(sql,args):
            d=result_event(con,row)
            if lat is not None:
                if not d['location_known']:continue
                d['distance_km']=round(distance_km(lat,lon,d['latitude'],d['longitude']),2)
                if d['distance_km']>radius:continue
            out.append(d)
    return {'items':out,'count':len(out)}


@app.get('/api/events/{event_id}')
def get_event(event_id:int):
    with db() as con:
        e=con.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
        if not e or e['status'] not in ('published','order'):raise HTTPException(404,'Событие не найдено')
        return result_event(con,e)


@app.post('/api/submissions')
def submit(payload: NewSubmission, session=Depends(member)):
    if (payload.latitude is None)!=(payload.longitude is None):
        raise HTTPException(422,'Координаты передаются парой')
    # New news is admitted to editor queue, NEVER directly public / map.
    with db() as con:
        event_id=payload.event_id
        if event_id is None:
            if payload.kind!='tip':raise HTTPException(422,'Для подтверждения/опровержения укажите событие')
            title=trim(payload.title,140) or trim(payload.text.split('.')[0],100)
            lat=payload.latitude if payload.latitude is not None else 55.7519
            lon=payload.longitude if payload.longitude is not None else 37.6176
            cur=con.execute('''INSERT INTO events(title,summary,topic,latitude,longitude,status,verification,importance,exclusive_score,created_at,updated_at)
                       VALUES(?,?,?,?,?,'review','unverified',50,50,?,?)''',(title,trim(payload.text,900),trim(payload.topic,60),lat,lon,now(),now()))
            event_id=cur.lastrowid
            create_draft(con,event_id)
        else:
            row=con.execute('SELECT status FROM events WHERE id=?',(event_id,)).fetchone()
            if not row:raise HTTPException(404,'Событие не найдено')
            if payload.kind in ('confirm','refute') and row['status']!='order':
                raise HTTPException(409,'Для подтверждения нужна открытая задача')
        cur=con.execute('''INSERT INTO submissions(event_id,user_id,kind,text,latitude,longitude,contact_opt_in,submitted_at)
                           VALUES(?,?,?,?,?,?,?,?)''',
                         (event_id,session['user_id'],payload.kind,trim(payload.text,3000),payload.latitude,payload.longitude,int(payload.contact_opt_in),now()))
        con.execute('UPDATE events SET updated_at=? WHERE id=?',(now(),event_id))
        sid=cur.lastrowid
    return {'id':sid,'event_id':event_id,'status':'pending','message':'Отправлено на проверку редактору'}


@app.get('/api/my/submissions')
def my_submissions(session=Depends(member)):
    with db() as con:
        user=user_view(con,session['user_id'],session['role'])
        out=rows_to_list(con.execute('''SELECT s.id,s.event_id,s.kind,s.text,s.status,s.submitted_at,e.title,
                (SELECT COALESCE(SUM(points),0) FROM rewards r WHERE r.submission_id=s.id) AS points,
                (SELECT COALESCE(SUM(money),0) FROM rewards r WHERE r.submission_id=s.id) AS money,
                (SELECT COUNT(*) FROM rewards r WHERE r.submission_id=s.id AND r.applied=0) AS reward_pending
                FROM submissions s JOIN events e ON e.id=s.event_id WHERE user_id=? ORDER BY s.id DESC LIMIT 100''',(session['user_id'],)))
    return {'items':out,'credits':user['credits'],'user':user}


@app.post('/api/submissions/{submission_id}/media')
async def upload_media(submission_id:int, file: UploadFile=File(...), session=Depends(member)):
    with db() as con:
        s=con.execute('SELECT user_id FROM submissions WHERE id=?',(submission_id,)).fetchone()
        if not s:raise HTTPException(404,'Сообщение не найдено')
        if s['user_id']!=session['user_id']:raise HTTPException(403,'Не ваше сообщение')
        count=con.execute('SELECT count(*) AS n FROM media WHERE submission_id=?',(submission_id,)).fetchone()['n']
        if count>=5:raise HTTPException(422,'Не более 5 файлов на сообщение')
    data=await file.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES:raise HTTPException(413,'Файл больше 12 МБ')
    if not data:raise HTTPException(422,'Пустой файл')
    return store_media_data(submission_id,data,file.content_type or '',file.filename or 'upload')


def store_media_data(submission_id:int,data:bytes,content:str,original_name:str):
    content=content or ''
    extension=''
    meta={'date_present':False,'gps_present':False,'note':'Наличие метаданных не доказывает подлинность события'}
    if content in ('image/jpeg','image/png','image/webp'):
        try:
            im=Image.open(io.BytesIO(data)); im.verify()
            im=Image.open(io.BytesIO(data))
            content={'JPEG':'image/jpeg','PNG':'image/png','WEBP':'image/webp'}.get(im.format,'')
            if not content:raise ValueError('Неизвестное изображение')
            exif=im.getexif()
            meta['date_present']=bool(exif.get(36867) or exif.get(306))
            meta['gps_present']=bool(exif.get(34853))
            extension={'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp'}[content]
        except (UnidentifiedImageError,ValueError,OSError):
            raise HTTPException(415,'Некорректный файл изображения')
    elif content=='video/mp4' and b'ftyp' in data[:32]:
        extension='.mp4'
    elif content=='video/webm' and data.startswith(b'\x1a\x45\xdf\xa3'):
        extension='.webm'
    else:
        raise HTTPException(415,'Разрешены JPG, PNG, WebP, MP4, WebM')
    filename=secrets.token_hex(24)+extension
    path=UPLOAD_DIR/filename
    path.write_bytes(data)
    with db() as con:
        cur=con.execute('''INSERT INTO media(submission_id,stored_name,original_name,mime,byte_size,exif_summary,created_at)
                   VALUES(?,?,?,?,?,?,?)''',(submission_id,filename,trim(original_name or 'upload',160),content,len(data),json.dumps(meta,ensure_ascii=False),now()))
        mid=cur.lastrowid
    return {'id':mid,'original_name':trim(original_name,160),'mime':content,'metadata_indicators':meta}


@app.get('/api/media/{media_id}')
def download_media(media_id:int, session=Depends(auth)):
    with db() as con:
        row=con.execute('''SELECT m.*,s.user_id FROM media m JOIN submissions s ON s.id=m.submission_id WHERE m.id=?''',(media_id,)).fetchone()
        if not row:raise HTTPException(404,'Медиа не найдено')
        if session['role'] not in ('editor','admin') and session['user_id']!=row['user_id']:
            raise HTTPException(403,'Нет доступа к медиа')
        filepath=UPLOAD_DIR/row['stored_name']
        if not filepath.exists():raise HTTPException(404,'Файл отсутствует')
        return FileResponse(filepath,media_type=row['mime'],headers={'Content-Disposition':'attachment; filename="media'+str(media_id)+'"','X-Content-Type-Options':'nosniff'})


def rule_analysis(con,event_id):
    e=con.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
    sub=rows_to_list(con.execute('SELECT kind,text,status FROM submissions WHERE event_id=?',(event_id,)))
    conf=sum(x['kind']=='confirm' for x in sub)
    refu=sum(x['kind']=='refute' for x in sub)
    flags=[]
    if not sub:flags.append('Нет пользовательских свидетельств')
    if conf and refu:flags.append('Есть противоречащие сообщения: требуется ручная проверка')
    if e['verification']!='verified':flags.append('Достоверность не установлена автоматически')
    if e['demo']:flags.append('Демонстрационное событие')
    summary=f"Тема: {e['topic']}. Потенциальная важность: {e['importance']}/100. Подтверждающих ответов: {conf}; опровержений: {refu}. Проверка источников и фактов человеком обязательна."
    con.execute('INSERT INTO analyses(event_id,summary,flags,created_at) VALUES(?,?,?,?)',(event_id,summary,json.dumps(flags,ensure_ascii=False),now()))
    return {'summary':summary,'flags':flags, 'method':'rule-based-demo','is_neural_network':False}


def create_draft(con,event_id):
    event=con.execute('SELECT title,summary,verification FROM events WHERE id=?',(event_id,)).fetchone()
    snippets=rows_to_list(con.execute("SELECT kind,text FROM submissions WHERE event_id=? AND status='accepted' ORDER BY id DESC LIMIT 8",(event_id,)))
    parts=[event['title'],'','Исходное сообщение: '+event['summary'],'','Статус: черновик редактора, факты требуют проверки.']
    if snippets:
        parts+=['','Новые сведения от пользователей (не являются установленными фактами):']
        parts += [('Подтверждение: ' if x['kind']=='confirm' else 'Опровержение: ' if x['kind']=='refute' else 'Дополнение: ')+x['text'] for x in snippets]
    current=con.execute('SELECT COALESCE(MAX(version),0) AS version FROM drafts WHERE event_id=?',(event_id,)).fetchone()['version']
    content='\n'.join(parts)
    con.execute('INSERT INTO drafts(event_id,version,content,created_at) VALUES(?,?,?,?)',(event_id,current+1,content,now()))
    return current+1


@app.post('/api/events/{event_id}/analyze')
async def analyze(event_id:int, _=Depends(editor)):
    with db() as con:
        row=con.execute('SELECT title,summary FROM events WHERE id=?',(event_id,)).fetchone()
        if not row:raise HTTPException(404,'Событие не найдено')
        submissions=rows_to_list(con.execute('SELECT kind,text FROM submissions WHERE event_id=?',(event_id,)))
        result=rule_analysis(con,event_id)
    if os.getenv('OPENAI_API_KEY',''):
        try:
            llm_result=await llm_triage(row['title'],row['summary'],submissions)
            if llm_result:
                with db() as con:
                    con.execute('UPDATE events SET importance=? WHERE id=?',(llm_result['importance'],event_id))
                    con.execute('INSERT INTO analyses(event_id,summary,flags,created_at) VALUES(?,?,?,?)',
                        (event_id,llm_result['editor_summary'],json.dumps(llm_result['risk_flags'],ensure_ascii=False),now()))
                result['llm']=llm_result
                result['method']='rule-based+llm-triage'
        except Exception as exc:
            result['llm_warning']='LLM недоступна: '+type(exc).__name__
    return result


@app.get('/api/editor/events')
def editor_events(_=Depends(editor)):
    with db() as con:
        items=[result_event(con,x) for x in con.execute('SELECT * FROM events ORDER BY updated_at DESC, id DESC LIMIT 200')]
        submissions=rows_to_list(con.execute('''SELECT s.*,e.title as event_title,
            (SELECT count(*) FROM media m WHERE m.submission_id=s.id) AS media_count,
            CASE WHEN s.contact_opt_in=1 THEN u.phone ELSE NULL END AS contact_phone
            FROM submissions s JOIN events e ON e.id=s.event_id LEFT JOIN users u ON u.id=s.user_id ORDER BY s.id DESC LIMIT 200'''))
        history=rows_to_list(con.execute('SELECT * FROM decisions ORDER BY id DESC LIMIT 100'))
        drafts=rows_to_list(con.execute('SELECT * FROM drafts ORDER BY id DESC LIMIT 100'))
        users=rows_to_list(con.execute('''SELECT u.id,COALESCE(u.nickname,'Очевидец #'||u.id) AS nickname,u.credits,u.wallet,
            (SELECT count(*) FROM submissions s WHERE s.user_id=u.id) AS submissions FROM users u ORDER BY u.credits DESC,u.id LIMIT 100'''))
    return {'events':items,'submissions':submissions,'history':history,'drafts':drafts,'users':users}


@app.post('/api/editor/events/{event_id}/decision')
def decision(event_id:int,payload: DecisionIn,_=Depends(editor)):
    if (payload.latitude is None)!=(payload.longitude is None):
        raise HTTPException(422, 'Координаты передаются парой')
    with db() as con:
        e=con.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
        if not e:raise HTTPException(404,'Событие не найдено')
        if payload.latitude is not None:
            con.execute('UPDATE events SET latitude=?,longitude=?,location_known=1 WHERE id=?', (payload.latitude,payload.longitude,event_id))
        target={'reject':'rejected','publish':'published','revise':'order'}[payload.action]
        # Публикация окончательна. После отклонения новость ещё можно опубликовать.
        if e['status']=='published':
            raise HTTPException(409,'Публикация окончательна: отменить её нельзя')
        # Повторное решение не создаёт новых задач/строк в истории даже при прямом API-вызове.
        if e['status']==target:
            raise HTTPException(409,'Это действие уже выполнено для новости')
        if payload.action=='publish' and e['verification']=='unverified':
            # Force a deliberate explicit sign-off instead of implying AI verified it.
            if 'проверено редактором' not in payload.note.lower():
                raise HTTPException(422,'Для публикации неподтверждённого сообщения нужно добавить в примечание «проверено редактором» после независимой проверки')
        if payload.action=='revise' and not trim(payload.question,1200):
            raise HTTPException(422,'Укажите, какие сведения требуется проверить')
        if payload.action=='revise':
            con.execute("UPDATE orders SET status='closed' WHERE event_id=? AND status='open'",(event_id,))
            con.execute('INSERT INTO orders(event_id,question,reward_points,created_at) VALUES(?,?,?,?)',(event_id,trim(payload.question,1200),CONFIRM_POINTS,now()))
        else:
            con.execute("UPDATE orders SET status='closed' WHERE event_id=? AND status='open'",(event_id,))
        importance=e['importance'] if payload.importance is None else payload.importance
        exclusive=e['exclusive_score'] if payload.exclusive_score is None else payload.exclusive_score
        con.execute('UPDATE events SET status=?,importance=?,exclusive_score=?,updated_at=? WHERE id=?',(target,importance,exclusive,now(),event_id))
        con.execute('INSERT INTO decisions(event_id,action,note,created_at) VALUES(?,?,?,?)',(event_id,payload.action,trim(payload.note,1600),now()))
        con.execute("UPDATE submissions SET status=? WHERE event_id=? AND kind='tip'",({'published':'accepted','rejected':'rejected'}.get(target,'pending'),event_id))
        # Автор новости: баллы и деньги только за достоверную; за недостоверную — минус баллы без денег.
        tip=con.execute("SELECT id,user_id FROM submissions WHERE event_id=? AND kind='tip' AND user_id IS NOT NULL ORDER BY id LIMIT 1",(event_id,)).fetchone()
        if tip:
            if target=='published':
                points,money,tier=news_reward(importance,exclusive)
                set_reward(con,tip,points,money,f'Достоверная новость · {tier}')
            elif target=='rejected':
                set_reward(con,tip,FALSE_NEWS_PENALTY,0,'Недостоверная новость')
            else:
                set_reward(con,tip,0,0,'Новость возвращена на проверку')
            
        analysis=rule_analysis(con,event_id)
    return {'status':target,'analysis':analysis}


@app.post('/api/editor/submissions/{submission_id}/review')
def review_submission(submission_id:int,payload: ReviewSubmission,_=Depends(editor)):
    with db() as con:
        row=con.execute('SELECT * FROM submissions WHERE id=?',(submission_id,)).fetchone()
        if not row:raise HTTPException(404,'Ответ не найден')
        if row['status']!='pending': raise HTTPException(409,'Ответ уже рассмотрен')
        status='accepted' if payload.accepted else 'rejected'
        con.execute('UPDATE submissions SET status=? WHERE id=?',(status,submission_id))
        rewarded=bool(row['user_id']) and row['kind'] in ('confirm','refute')
        if rewarded:
            set_reward(con,row,CONFIRM_POINTS if payload.accepted else CONFIRM_PENALTY,0,'Подтверждение принято' if payload.accepted else 'Подтверждение отклонено')
        order_completed=False
        if payload.accepted and row['kind'] in ('confirm','refute'):
            open_order=con.execute("SELECT id FROM orders WHERE event_id=? AND status='open' ORDER BY id DESC LIMIT 1",(row['event_id'],)).fetchone()
            if open_order:
                con.execute("UPDATE orders SET status='closed' WHERE id=?",(open_order['id'],))
                con.execute("UPDATE events SET status='review' WHERE id=?",(row['event_id'],))
                con.execute('INSERT INTO decisions(event_id,action,note,created_at) VALUES(?,?,?,?)',
                    (row['event_id'],'order_fulfilled','Задача закрыта: принято новое доказательство; нужен повторный анализ и решение редактора',now()))
                order_completed=True
        version=create_draft(con,row['event_id']) if payload.accepted else None
        con.execute('UPDATE events SET updated_at=? WHERE id=?',(now(),row['event_id']))
        if order_completed:rule_analysis(con,row['event_id'])
        return {'status':status,'awarded_credits':(CONFIRM_POINTS if payload.accepted else CONFIRM_PENALTY) if rewarded else 0,
                'order_completed':order_completed,'new_draft_version':version}


@app.get('/api/events/{event_id}/history')
def event_history(event_id:int,_=Depends(editor)):
    with db() as con:
        if not con.execute('SELECT id FROM events WHERE id=?',(event_id,)).fetchone(): raise HTTPException(404,'Нет события')
        return {'decisions':rows_to_list(con.execute('SELECT * FROM decisions WHERE event_id=? ORDER BY id DESC',(event_id,))),
                'analyses':rows_to_list(con.execute('SELECT * FROM analyses WHERE event_id=? ORDER BY id DESC',(event_id,))),
                'submissions':rows_to_list(con.execute('SELECT id,kind,text,status,submitted_at FROM submissions WHERE event_id=?',(event_id,)))}


@app.post('/api/discovery/search')
async def discovery(payload: DiscoveryIn,_=Depends(editor)):
    """Optional live web discovery with licensed Brave Search API, no crawling of private chats."""
    key=os.getenv('BRAVE_SEARCH_API_KEY','')
    import httpx
    if not key:
        from app.newsroom import fetch_feed, plain, search_url, web_url
        try:
            async with httpx.AsyncClient() as client:
                entries, _ = await fetch_feed(client, search_url(payload.query))
            return {'query':payload.query, 'items':[{'title':plain(e.get('title'),180), 'url':e.get('link'), 'description':plain(e.get('summary'))} for e in (entries or [])[:8] if web_url(e.get('link',''))],
                    'provider':'google-news-rss', 'note':'Результаты требуют редакторской проверки'}
        except Exception as exc:
            raise HTTPException(502, f'Поиск новостей недоступен ({type(exc).__name__})')
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response=await client.get('https://api.search.brave.com/res/v1/web/search',params={'q':payload.query,'count':8},headers={'X-Subscription-Token':key,'Accept':'application/json'})
            response.raise_for_status()
            data=response.json()
    except Exception as exc:
        raise HTTPException(502,f'Поисковый сервис недоступен ({type(exc).__name__})')
    items=[]
    for e in data.get('web',{}).get('results',[])[:8]:
        items.append({'title':e.get('title',''),'url':e.get('url',''),'description':e.get('description','')})
    return {'query':payload.query,'items':items,'note':'Поисковые результаты не являются проверенными фактами; редактор решает, добавлять ли их в события.'}



class ImportDiscovery(BaseModel):
    title: str = Field(min_length=8,max_length=180)
    summary: str = Field(default='',max_length=1200)
    source_url: str = Field(min_length=8,max_length=900)
    topic: str = 'Город'
    latitude: float = Field(default=55.7519,ge=-90,le=90)
    longitude: float = Field(default=37.6176,ge=-180,le=180)


@app.post('/api/discovery/import')
def import_discovery(payload:ImportDiscovery,_=Depends(editor)):
    """Editor imports one public search hit as a draft in human review queue."""
    from difflib import SequenceMatcher
    url=payload.source_url.strip()
    if not url.startswith(('https://','http://')):
        raise HTTPException(422,'Источник должен быть HTTP(S)-ссылкой')
    with db() as con:
        near=con.execute('SELECT id,title FROM events ORDER BY id DESC LIMIT 100').fetchall()
        for e in near:
            similarity=SequenceMatcher(None,payload.title.lower(),e['title'].lower()).ratio()
            if similarity>0.87:
                return {'imported':False,'matched_event_id':e['id'],'reason':'Возможный дубль, проверьте вручную'}
        cur=con.execute("""INSERT INTO events(title,summary,topic,latitude,longitude,status,verification,source_url,importance,exclusive_score,created_at,updated_at)
            VALUES(?,?,?,?,?,'review','unverified',?,50,65,?,?)""",
            (payload.title,trim(payload.summary,1200),trim(payload.topic,60),payload.latitude,payload.longitude,url,now(),now()))
        con.execute('UPDATE events SET location_known=0 WHERE id=?', (cur.lastrowid,))
    return {'imported':True,'event_id':cur.lastrowid,'status':'review'}


# Common ingestion endpoint for approved external bot adapters. Not public without secret.
class BotIn(BaseModel):
    provider: Literal['telegram','max','vk','whatsapp']
    provider_user_id: str
    text: str = Field(min_length=5,max_length=3000)
    latitude: float|None = Field(None,ge=-90,le=90)
    longitude: float|None = Field(None,ge=-180,le=180)
    message_id: int|None = None
    chat_id: int|None = None


@app.post('/api/integrations/ingest')
def ingest_bot(payload: BotIn, x_ingest_key: Annotated[str|None,Header(alias='X-Ingest-Key')]=None):
    expected=os.getenv('INGEST_SECRET','')
    if not expected or not x_ingest_key or not hmac.compare_digest(expected,x_ingest_key):
        raise HTTPException(403,'Интеграция не настроена')
    return create_bot_submission(payload)


def create_bot_submission(payload: BotIn):
    if (payload.latitude is None)!=(payload.longitude is None):raise HTTPException(422,'Координаты передаются парой')
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        if payload.provider == 'telegram' and payload.message_id is not None and payload.chat_id is not None:
            previous = con.execute('SELECT * FROM telegram_messages WHERE chat_id=? AND message_id=?', (payload.chat_id, payload.message_id)).fetchone()
            if previous:
                return {'ok': True, 'event_id': previous['event_id'], 'submission_id': previous['submission_id'], 'status': 'review'}
        lat=payload.latitude if payload.latitude is not None else 55.7519
        lon=payload.longitude if payload.longitude is not None else 37.6176
        title=trim(payload.text.split('.')[0],115)
        cur=con.execute("INSERT INTO events(title,summary,topic,latitude,longitude,status,verification,created_at,updated_at) VALUES(?,?,?,?,?,'review','unverified',?,?)",
                (title,trim(payload.text,900),'Город',lat,lon,now(),now()))
        eid=cur.lastrowid
        sent=con.execute('INSERT INTO submissions(event_id,provider,provider_user_id,kind,text,latitude,longitude,submitted_at) VALUES(?,?,?,\'tip\',?,?,?,?)',
                (eid,payload.provider,trim(payload.provider_user_id,120),trim(payload.text,3000),payload.latitude,payload.longitude,now()))
        sid=sent.lastrowid
        if payload.provider == 'telegram' and payload.message_id is not None and payload.chat_id is not None:
            con.execute('INSERT INTO telegram_messages(chat_id,message_id,event_id,submission_id) VALUES(?,?,?,?)', (payload.chat_id, payload.message_id, eid, sid))
    return {'ok':True,'event_id':eid,'submission_id':sid,'status':'review'}


@app.post('/api/integrations/media/{submission_id}')
async def ingest_bot_media(submission_id:int, file: UploadFile=File(...),
        x_ingest_key: Annotated[str|None,Header(alias='X-Ingest-Key')]=None):
    """Trusted bot adapter can attach incoming media to an already ingested submission.

    A production version should additionally enforce provider-specific identities,
    malware scanning, stricter quotas and media retention limits.
    """
    expected=os.getenv('INGEST_SECRET','')
    if not expected or not x_ingest_key or not hmac.compare_digest(expected,x_ingest_key):
        raise HTTPException(403,'Интеграция не настроена')
    with db() as con:
        row=con.execute('SELECT provider FROM submissions WHERE id=?',(submission_id,)).fetchone()
        if not row or row['provider']=='web':raise HTTPException(404,'Сообщение бота не найдено')
        if con.execute('SELECT count(*) AS n FROM media WHERE submission_id=?',(submission_id,)).fetchone()['n']>=5:
            raise HTTPException(422,'Не более 5 файлов')
    data=await file.read(MAX_BYTES+1)
    if not data or len(data)>MAX_BYTES:raise HTTPException(413,'Пустой или слишком большой файл')
    return store_media_data(submission_id,data,file.content_type or '',file.filename or 'bot_upload')


@app.post('/hooks/max')
async def max_webhook(request: Request):
    """MAX message_created webhook, requires MAX_WEBHOOK_SECRET configured on subscription."""
    expected=os.getenv('MAX_WEBHOOK_SECRET','')
    provided=request.headers.get('X-Max-Bot-Api-Secret','')
    if not expected or not hmac.compare_digest(expected,provided):
        raise HTTPException(403,'Подпись MAX не совпадает')
    data=await request.json()
    if data.get('update_type')!='message_created':
        return {'ok':True,'ignored':True}
    msg=data.get('message') or {}
    text=trim((msg.get('body') or {}).get('text'),3000)
    uid=str((msg.get('sender') or {}).get('user_id',''))
    if len(text)<5 or not uid:
        return {'ok':True,'ignored':True}
    return ingest_bot(BotIn(provider='max',provider_user_id=uid,text=text),x_ingest_key=os.getenv('INGEST_SECRET',''))


@app.post('/hooks/vk')
async def vk_webhook(request:Request):
    """VK Callback API text ingestion; configure secret and confirmation code in VK panel."""
    data=await request.json()
    expected=os.getenv('VK_WEBHOOK_SECRET','')
    if not expected or not hmac.compare_digest(str(data.get('secret','')),expected):
        raise HTTPException(403,'Неверный секрет VK')
    if data.get('type')=='confirmation':
        return JSONResponse(content=os.getenv('VK_CONFIRMATION_CODE',''),media_type='text/plain')
    if data.get('type')!='message_new':return {'ok':True,'ignored':True}
    m=(data.get('object') or {}).get('message') or {}
    content=trim(m.get('text'),3000)
    uid=str(m.get('from_id',''))
    if len(content)>=5 and uid:
        ingest_bot(BotIn(provider='vk',provider_user_id=uid,text=content),x_ingest_key=os.getenv('INGEST_SECRET',''))
    return JSONResponse(content='ok',media_type='text/plain')


@app.get('/hooks/whatsapp')
def whatsapp_challenge(request:Request):
    expected=os.getenv('WHATSAPP_VERIFY_TOKEN','')
    if not expected or not hmac.compare_digest(request.query_params.get('hub.verify_token',''),expected):
        raise HTTPException(403,'Webhook не подтверждён')
    if request.query_params.get('hub.mode')!='subscribe':raise HTTPException(403,'Неверный режим')
    return JSONResponse(content=request.query_params.get('hub.challenge',''),media_type='text/plain')


@app.post('/hooks/whatsapp')
async def whatsapp_webhook(request:Request):
    """WhatsApp Cloud API: verifies HMAC; MVP ingests plain text only."""
    secret=os.getenv('WHATSAPP_APP_SECRET','')
    if not secret:raise HTTPException(403,'WhatsApp не подключён')
    raw=await request.body()
    signature=request.headers.get('X-Hub-Signature-256','')
    expected='sha256='+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,signature):raise HTTPException(403,'Неверная подпись WhatsApp')
    data=json.loads(raw)
    for entry in data.get('entry',[]):
        for change in entry.get('changes',[]):
            for message in (change.get('value') or {}).get('messages',[]):
                content=trim((message.get('text') or {}).get('body'),3000)
                uid=str(message.get('from',''))
                if len(content)>=5 and uid:
                    ingest_bot(BotIn(provider='whatsapp',provider_user_id=uid,text=content),x_ingest_key=os.getenv('INGEST_SECRET',''))
    return {'ok':True}


@app.middleware('http')
async def revalidate_pages(request: Request, call_next):
    """Versioned assets stay in the WebView cache; HTML is always revalidated."""
    response=await call_next(request)
    if request.url.path.startswith('/static/') and (request.query_params.get('v') == app.version or request.url.path.startswith('/static/vendor/leaflet-1.9.4/')):
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif request.url.path in ('/','/editor') or request.url.path.startswith('/static/'):
        response.headers.setdefault('Cache-Control','no-cache')
    return response


@app.get('/health')
def health():
    return {'ok':True,'version':app.version,'demo_mode':DEMO_MODE,
            'worker_enabled':os.getenv('NEWS_WORKER_ENABLED','1')=='1',
            'telegram_configured':bool(os.getenv('TELEGRAM_BOT_TOKEN'))}


@app.get('/')
def index():
    return FileResponse(STATIC/'index.html')


@app.get('/editor')
def editor_page():
    return FileResponse(STATIC/'editor.html')


from app.newsroom import Newsroom

newsroom = Newsroom(db, auth, editor, make_session, user_view)
newsroom.init()
app.include_router(newsroom.router)

app.mount('/static',StaticFiles(directory=str(STATIC)),name='static')

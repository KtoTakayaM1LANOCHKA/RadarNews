import asyncio
import hashlib
import hmac
import io
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from PIL import Image

from app import main
from app.newsroom import Newsroom, public_url, fetch_feed, SourceIn, search_url
from app.bots.telegram_bot import TelegramBot, TelegramError


def signed_data(uid=555, age=0, token='123:test-token'):
    fields = {'auth_date': str(int(time.time())-age), 'query_id': 'query', 'user': json.dumps({'id': uid, 'first_name': 'Читатель'})}
    key = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(key, '\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def link(api, account, tg_id=555):
    r=api.post('/api/my/telegram/link', headers=account['headers'])
    assert r.status_code==200, r.text
    return main.newsroom.bind_chat(r.json()['code'],tg_id,tg_id)


def submit(api, account, text='Тестовое описание события'):
    r=api.post('/api/submissions', headers=account['headers'], json={'text':text,'title':'Новость для проверки'})
    assert r.status_code==200, r.text
    return r.json()


def test_access_revocation_is_immediate(api, accounts):
    editor=accounts['editor']
    assert api.get('/api/editor/events',headers=editor['headers']).status_code==200
    assert api.get('/api/my/submissions',headers=editor['headers']).status_code==200
    assert api.patch(f"/api/admin/users/{editor['id']}/role",headers=accounts['user']['headers'],json={'role':'editor'}).status_code==403
    assert api.patch(f"/api/admin/users/{accounts['admin']['id']}/role",headers=accounts['admin']['headers'],json={'role':'user'}).status_code==409
    api.patch(f"/api/admin/users/{editor['id']}/role",headers=accounts['admin']['headers'],json={'role':'user'})
    assert api.get('/api/editor/events',headers=editor['headers']).status_code==403
    assert api.get('/api/auth/me',headers=editor['headers']).json()['role']=='user'


def test_cabinet_editor_password_binds_account(api, accounts):
    r=api.post('/api/editor/login',headers=accounts['user']['headers'],json={'password':main.EDITOR_PASSWORD})
    assert r.status_code==200
    assert r.json()['token']==accounts['user']['token']
    assert api.get('/api/auth/me',headers=accounts['user']['headers']).json()['role']=='editor'
    assert api.get('/api/my/submissions',headers=accounts['user']['headers']).status_code==200


def test_link_is_editor_only_one_time_private_and_expires(api, accounts):
    assert api.post('/api/my/telegram/link',headers=accounts['user']['headers']).status_code==403
    r=api.post('/api/my/telegram/link',headers=accounts['editor']['headers']).json()
    assert main.newsroom.bind_chat(r['code'],555,555)
    assert not main.newsroom.bind_chat(r['code'],555,555)
    r=api.post('/api/my/telegram/link',headers=accounts['editor']['headers']).json()
    with main.db() as con:con.execute('UPDATE telegram_link_codes SET expires_at=0')
    assert not main.newsroom.bind_chat(r['code'],555,555)
    api.delete('/api/my/telegram',headers=accounts['editor']['headers'])
    assert not api.get('/api/my/telegram',headers=accounts['editor']['headers']).json()['linked']


@pytest.mark.parametrize('raw',[signed_data(age=601),signed_data(token='wrong'),signed_data()+'&hash=duplicate','bad=data'])
def test_invalid_telegram_auth(api,raw):
    assert api.post('/api/auth/telegram',json={'init_data':raw}).status_code==401


def test_miniapp_auth_uses_linked_roles_and_normal_features(api, accounts):
    assert link(api,accounts['editor'])
    session=api.post('/api/auth/telegram',json={'init_data':signed_data()}).json()
    assert session['user']['id']==accounts['editor']['id']
    assert session['user']['role']=='editor'
    headers={'X-Session':session['token']}
    r=api.post('/api/submissions',headers=headers,json={'text':'Сообщение из Mini App','title':'Новость Mini App'})
    assert r.status_code==200
    assert api.get('/api/my/submissions',headers=headers).json()['items'][0]['event_id']==r.json()['event_id']
    api.patch(f"/api/admin/users/{accounts['editor']['id']}/role",headers=accounts['admin']['headers'],json={'role':'user'})
    assert api.get('/api/editor/events',headers=headers).status_code==403


def test_unlinked_telegram_cannot_claim_editor(api):
    first=api.post('/api/auth/telegram',json={'init_data':signed_data()}).json()
    second=api.post('/api/auth/telegram',json={'init_data':signed_data()}).json()
    assert first['user']['id']==second['user']['id']
    assert first['user']['role']=='user'
    assert api.get('/api/editor/events',headers={'X-Session':first['token']}).status_code==403


def test_source_permissions_validation_and_update(api,accounts):
    assert api.get('/api/editor/sources').status_code==401
    assert api.post('/api/editor/sources',headers=accounts['user']['headers'],json={'name':'Test','query':'новости'}).status_code==403
    for payload in [{'kind':'rss','url':'file:///etc/passwd'}, {'kind':'search','query':'a'}, {'query':'новости','interval_minutes':0}]:
        assert api.post('/api/editor/sources',headers=accounts['editor']['headers'],json={'name':'Test',**payload}).status_code==422
    payload={'name':'Транспорт','query':'новости Казани транспорт','enabled':False}
    r=api.post('/api/editor/sources',headers=accounts['editor']['headers'],json=payload)
    assert r.status_code==200, r.text
    assert api.put('/api/editor/sources/'+str(r.json()['id']),headers=accounts['editor']['headers'],json={**payload,'enabled':True}).status_code==200


def test_dedup_and_unknown_location_stays_out_of_public_map(api,accounts):
    with main.db() as con:source=dict(con.execute('SELECT * FROM news_sources LIMIT 1').fetchone())
    entries=[{'title':'<b>Новости транспорта</b>','link':'https://example.org/one','summary':'<p>Описание события</p>'}]*2
    assert main.newsroom.import_entries(source,entries)==1
    assert main.newsroom.import_entries(source,entries)==0
    with main.db() as con:
        event=dict(con.execute('SELECT * FROM events WHERE source_url=?',('https://example.org/one',)).fetchone())
        assert event['status']=='review' and event['verification']=='unverified'
        assert event['title']=='Новости транспорта'
    assert not any(e['id']==event['id'] for e in api.get('/api/events').json()['items'])
    r=api.post(f"/api/editor/events/{event['id']}/decision",headers=accounts['editor']['headers'],json={'action':'publish','note':'Проверено редактором'})
    assert r.status_code==200
    assert not any(e['id']==event['id'] for e in api.get('/api/events?lat=55.7519&lon=37.6176').json()['items'])


@pytest.mark.asyncio
async def test_collection_persists_errors_and_dedup(api,accounts,monkeypatch):
    from app import newsroom as module
    async def feed(*args,**kwargs):
        return [{'title':'Свежая городская новость','link':'https://example.org/fresh','summary':'Новое событие'}],{'etag':'test-etag'}
    monkeypatch.setattr(module,'fetch_feed',feed)
    async with httpx.AsyncClient() as client:
        assert (await main.newsroom.collect(client,force=True))['imported']==1
        assert (await main.newsroom.collect(client,force=True))['imported']==0
        async def failed(*args,**kwargs):raise ValueError('Лента больше 2 МБ')
        monkeypatch.setattr(module,'fetch_feed',failed)
        assert len((await main.newsroom.collect(client,force=True))['errors'])==1
    with main.db() as con:
        row=con.execute('SELECT * FROM news_sources LIMIT 1').fetchone()
        assert row['imported_count']==1 and row['etag']=='test-etag' and row['last_error']


@pytest.mark.asyncio
async def test_local_and_redirected_private_urls_blocked(api,monkeypatch):
    for url in ['http://127.0.0.1/x','http://[::1]/rss','http://169.254.169.254/meta','file:///etc/passwd','https://user:pass@example.org/rss']:
        with pytest.raises(ValueError):await public_url(url)
    from app import newsroom as module
    seen=[]
    async def validate(url):
        seen.append(url)
        if '127.0.0.1' in url:raise ValueError('Private')
    monkeypatch.setattr(module,'public_url',validate)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(302,headers={'location':'http://127.0.0.1/private'}))) as client:
        with pytest.raises(ValueError):await fetch_feed(client,'https://example.org/rss')
    assert len(seen)==2


@pytest.mark.asyncio
async def test_rss_atom_size_and_not_html(api,monkeypatch):
    from app import newsroom as module
    async def allow(url):pass
    monkeypatch.setattr(module,'public_url',allow)
    bodies=[b'<rss version="2.0"><channel><title>T</title><item><title>Test news</title><link>https://example.org/a</link></item></channel></rss>', b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title><entry><title>Atom news</title><link href="https://example.org/b"/></entry></feed>']
    for body in bodies:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=body))) as c:
            entries,_=await fetch_feed(c,'https://example.org/feed')
            assert len(entries)==1
    for body in [b'<html>not a feed</html>',b'x'*(module.MAX_FEED_BYTES+1)]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=body))) as c:
            with pytest.raises(ValueError):await fetch_feed(c,'https://example.org/feed')


@pytest.mark.asyncio
async def test_notification_queued_retried_sent_once_and_revoke(api,accounts,monkeypatch):
    assert link(api,accounts['editor'])
    created=submit(api,accounts['user'])
    async with httpx.AsyncClient() as client:
        bot=TelegramBot(main.newsroom,client)
        async def failed(*args,**kwargs):raise TelegramError(429,120)
        monkeypatch.setattr(bot,'send',failed)
        await bot.deliver()
        with main.db() as con:
            row=dict(con.execute('SELECT * FROM telegram_deliveries').fetchone())
            assert row['status']=='pending' and row['attempts']==1 and row['next_try']>=time.time()+110
            con.execute('UPDATE telegram_deliveries SET next_try=0')
        sent=[]
        async def send(*args,**kwargs):sent.append(args)
        monkeypatch.setattr(bot,'send',send)
        await bot.deliver();await bot.deliver()
        assert len(sent)==1 and sent[0][0]==555
        submit(api,accounts['user'],'Ещё одна новость для очереди')
        api.patch(f"/api/admin/users/{accounts['editor']['id']}/role",headers=accounts['admin']['headers'],json={'role':'user'})
        await bot.deliver()
        assert len(sent)==1


@pytest.mark.asyncio
async def test_bot_intake_idempotent_media_and_stop(api,accounts,monkeypatch):
    assert link(api,accounts['editor'])
    async with httpx.AsyncClient() as client:
        bot=TelegramBot(main.newsroom,client)
        sent=[]
        async def send(*args,**kwargs):sent.append(args)
        monkeypatch.setattr(bot,'send',send)
        msg={'message':{'chat':{'id':555,'type':'private'},'from':{'id':555,'first_name':'Редактор'},'message_id':10,'text':'Сообщение редактору из бота'}}
        await bot.handle(msg);await bot.handle(msg)
        with main.db() as con:
            assert con.execute("SELECT count(*) n FROM submissions WHERE provider='telegram'").fetchone()['n']==1
            sub=dict(con.execute("SELECT * FROM submissions WHERE provider='telegram'").fetchone())
            assert sub['user_id']==accounts['editor']['id']
        await bot.handle({'message':{**msg['message'],'text':'/stop'}})
        submit(api,accounts['user'])
        with main.db() as con:
            assert con.execute("SELECT count(*) n FROM telegram_deliveries WHERE status='pending'").fetchone()['n']==0
        before=len(sent)
        await bot.handle({'message':{**msg['message'],'chat':{'id':-1,'type':'group'},'text':'/start fake'}})
        assert len(sent)==before


@pytest.mark.asyncio
async def test_new_telegram_reader_history_visible_in_miniapp(api,monkeypatch):
    async with httpx.AsyncClient() as client:
        bot=TelegramBot(main.newsroom,client)
        async def send(*args,**kwargs):pass
        monkeypatch.setattr(bot,'send',send)
        await bot.handle({'message':{'chat':{'id':555,'type':'private'},'from':{'id':555,'first_name':'Читатель'},'message_id':1,'text':'История сообщения для Mini App'}})
    session=api.post('/api/auth/telegram',json={'init_data':signed_data()}).json()
    items=api.get('/api/my/submissions',headers={'X-Session':session['token']}).json()['items']
    assert len(items)==1 and items[0]['text']=='История сообщения для Mini App'


def test_worker_lock_excludes_other_process(api):
    other=Newsroom(main.db,main.auth,main.editor,main.make_session,main.user_view)
    assert main.newsroom.acquire('poll-test')
    assert not other.acquire('poll-test')
    main.newsroom.release('poll-test')
    assert other.acquire('poll-test')


def test_existing_moderation_rewards_and_upload(api,accounts):
    created=submit(api,accounts['user'])
    image=Image.new('RGB',(2,2));body=io.BytesIO();image.save(body,format='PNG')
    r=api.post(f"/api/submissions/{created['id']}/media",headers=accounts['user']['headers'],files={'file':('photo.png',body.getvalue(),'image/png')})
    assert r.status_code==200
    r=api.post(f"/api/editor/events/{created['event_id']}/decision",headers=accounts['editor']['headers'],json={'action':'publish','note':'Проверено редактором','importance':80,'exclusive_score':80})
    assert r.status_code==200
    cabinet=api.get('/api/my/submissions',headers=accounts['user']['headers']).json()
    assert cabinet['user']['pending_points']>0 and cabinet['user']['pending_money']>0
    assert api.get(f"/api/events/{created['event_id']}").status_code==200

@pytest.mark.asyncio
async def test_bot_media_uses_existing_storage_and_size_limit(api,accounts,monkeypatch):
    from app.newsroom import stamp
    image=Image.new('RGB',(3,3));body=io.BytesIO();image.save(body,format='PNG')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=body.getvalue()))) as client:
        bot=TelegramBot(main.newsroom,client)
        async def tg(method,**fields):return {'file_path':'photos/test.png','file_size':len(body.getvalue())}
        async def send(*args,**kwargs):pass
        monkeypatch.setattr(bot,'tg',tg);monkeypatch.setattr(bot,'send',send)
        msg={'message':{'chat':{'id':555,'type':'private'},'from':{'id':555,'first_name':'Автор'},'message_id':50,'caption':'Фото новости с описанием','photo':[{'file_id':'photo','file_size':len(body.getvalue())}]}}
        await bot.handle(msg);await bot.handle(msg)
        with main.db() as con:
            assert con.execute('SELECT count(*) n FROM media').fetchone()['n']==1
        with pytest.raises(ValueError):await bot.upload(1,{'file_id':'huge','file_size':13*1024*1024})


@pytest.mark.asyncio
async def test_blocked_bot_disables_subscription(api,accounts,monkeypatch):
    assert link(api,accounts['editor'])
    submit(api,accounts['user'])
    async with httpx.AsyncClient() as client:
        bot=TelegramBot(main.newsroom,client)
        async def blocked(*args,**kwargs):raise TelegramError(403)
        monkeypatch.setattr(bot,'send',blocked)
        await bot.deliver()
    with main.db() as con:
        assert con.execute('SELECT enabled FROM telegram_links').fetchone()['enabled']==0
        assert con.execute('SELECT status FROM telegram_deliveries').fetchone()['status']=='failed'


@pytest.mark.asyncio
async def test_poll_offset_persists_after_handling(api,monkeypatch):
    async with httpx.AsyncClient() as client:
        bot=TelegramBot(main.newsroom,client)
        polls=[]
        async def tg(method,**fields):
            if method=='getMe':return {'username':'test_bot'}
            if method=='getUpdates':
                polls.append(fields.get('offset'))
                if len(polls)>1:raise asyncio.CancelledError
                return [{'update_id':10,'message':{'chat':{'id':555,'type':'private'},'from':{'id':555},'message_id':12,'text':'/app'}}]
            return True
        monkeypatch.setattr(bot,'tg',tg)
        with pytest.raises(asyncio.CancelledError):await bot.poll()
        assert polls==[None,11]
        with main.db() as con:
            assert con.execute("SELECT value FROM worker_state WHERE key='telegram-offset'").fetchone()['value']=='11'


def test_evidence_notification_only_to_editors(api,accounts):
    assert link(api,accounts['editor'])
    assert link(api,accounts['admin'],tg_id=777)
    with main.db() as con:
        eid=con.execute("SELECT id FROM events WHERE status='order' LIMIT 1").fetchone()['id']
    r=api.post('/api/submissions',headers=accounts['user']['headers'],json={'event_id':eid,'kind':'confirm','text':'Уже имеющееся подтверждение события'})
    assert r.status_code==200
    with main.db() as con:
        jobs=con.execute("SELECT * FROM telegram_deliveries WHERE kind='evidence'").fetchall()
        assert {r['chat_id'] for r in jobs}=={555,777}
        assert all(job['submission_id']==r.json()['id'] for job in jobs)


def test_search_without_key_and_manual_import(api,accounts,monkeypatch):
    from app import newsroom as module
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','')
    async def feed(*args,**kwargs):return [{'title':'Новость из интернета','link':'https://example.org/discovery','summary':'Описание из интернета'}],{}
    monkeypatch.setattr(module,'fetch_feed',feed)
    r=api.post('/api/discovery/search',headers=accounts['editor']['headers'],json={'query':'события рядом'})
    assert r.status_code==200, r.text
    data=r.json();assert data['provider']=='google-news-rss'
    hit=data['items'][0]
    r=api.post('/api/discovery/import',headers=accounts['editor']['headers'],json={'title':hit['title'],'summary':hit['description'],'source_url':hit['url']})
    assert r.status_code==200 and r.json()['imported']
    with main.db() as con:
        assert con.execute('SELECT location_known FROM events WHERE id=?',(r.json()['event_id'],)).fetchone()['location_known']==0


def test_publication_with_verified_location(api,accounts):
    with main.db() as con:source=dict(con.execute('SELECT * FROM news_sources LIMIT 1').fetchone())
    main.newsroom.import_entries(source,[{'title':'Новость с проверенным местом','link':'https://example.org/located'}])
    with main.db() as con:eid=con.execute('SELECT id FROM events WHERE source_url=?',('https://example.org/located',)).fetchone()['id']
    r=api.post(f'/api/editor/events/{eid}/decision',headers=accounts['editor']['headers'],json={'action':'publish','note':'Проверено редактором','latitude':55.7519,'longitude':37.6176})
    assert r.status_code==200, r.text
    assert any(e['id']==eid for e in api.get('/api/events?lat=55.7519&lon=37.6176').json()['items'])


def test_compressed_and_versioned_assets(api):
    version = api.get('/health').json()['version']
    html = api.get('/')
    assert html.headers['cache-control'] == 'no-cache'
    assert f'/static/app.js?v={version}' in html.text
    assert 'telegram.org/js/telegram-web-app.js' not in html.text
    assert 'id="boot"' in html.text
    js = api.get(f'/static/app.js?v={version}', headers={'Accept-Encoding':'gzip'})
    assert js.headers['content-encoding'] == 'gzip'
    assert 'immutable' in js.headers['cache-control']
    assert api.get('/static/app.js').headers['cache-control'] == 'no-cache'
    assert api.get('/static/app.js?v=old').headers['cache-control'] == 'no-cache'
    vendor = api.get('/static/vendor/leaflet-1.9.4/leaflet.js')
    assert vendor.status_code == 200 and 'immutable' in vendor.headers['cache-control']

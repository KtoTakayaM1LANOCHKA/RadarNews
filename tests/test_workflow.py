import os
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

# Set isolation before importing application
TEST_ROOT = Path(tempfile.mkdtemp(prefix='nr-test-'))
os.environ['DB_PATH'] = str(TEST_ROOT/'test.db')
os.environ['UPLOAD_DIR'] = str(TEST_ROOT/'uploads')
os.environ['DEMO_MODE'] = '1'
os.environ['DEMO_OTP'] = '1'
os.environ['EDITOR_PASSWORD'] = 'test-secret-editor'
os.environ['INGEST_SECRET'] = 'test-secret-ingest'
os.environ['MAX_WEBHOOK_SECRET'] = 'test-max-secret'
os.environ['VK_WEBHOOK_SECRET'] = 'test-vk-secret'
os.environ['VK_CONFIRMATION_CODE'] = 'test-vk-confirm'

from app.main import app

client=TestClient(app)


def make_user(phone='+79990000001'):
    a=client.post('/api/auth/request-code',json={'phone':phone})
    assert a.status_code==200,a.text
    code=a.json()['demo_code']
    b=client.post('/api/auth/verify-code',json={'phone':phone,'code':code})
    assert b.status_code==200,b.text
    return {'X-Session':b.json()['token']}


def make_rewards_due():
    """Имитирует наступление следующего дня: все ожидающие начисления становятся доступными."""
    from app.main import db
    with db() as con:
        con.execute('UPDATE rewards SET available_at=0 WHERE applied=0')


def make_editor():
    a=client.post('/api/editor/login',json={'password':'test-secret-editor'})
    assert a.status_code==200,a.text
    return {'X-Session':a.json()['token']}


def test_pages():
    assert client.get('/').status_code==200
    conf=client.get('/api/config').json()
    assert conf['map_provider']=='osm_leaflet'
    assert 'yandex_maps_api_key' not in conf
    js=client.get('/static/ui.js').text
    assert 'L.tileLayer' in js and 'tile.openstreetmap.org' in js
    assert 'L.marker' in js
    assert client.get('/static/editor.js').status_code==200
    assert client.get('/editor').status_code==200
    assert client.get('/static/app.js').status_code==200
    assert client.get('/health').json()['ok']
    assert client.get('/api/events').status_code==200
    assert client.get('/api/events?status=review').json()['count']==0


def test_editor_order_cycle():
    u=make_user('+79990000002'); e=make_editor()
    tip=client.post('/api/submissions',headers=u,json={'title':'Возможное изменение городского расписания','text':'В локальном объявлении указали изменения движения автобусов','topic':'Транспорт','kind':'tip'})
    assert tip.status_code==200,tip.text
    event=tip.json()['event_id']
    assert client.get(f'/api/events/{event}').status_code==404
    assert client.get('/api/editor/events',headers=u).status_code==403
    decision=client.post(f'/api/editor/events/{event}/decision',headers=e,json={'action':'revise','question':'Нужен официальный документ или ссылка'} )
    assert decision.status_code==200,decision.text
    assert client.get(f'/api/events/{event}').status_code==200
    public=client.get('/api/events?status=order&lat=55.75&lon=37.62&radius=10').json()['items']
    assert any(x['id']==event for x in public)
    # Both refutation and confirmation accepted as possible responses.
    response=client.post('/api/submissions',headers=u,json={'event_id':event,'kind':'refute','text':'Найдено сообщение о сохранении прежнего расписания'})
    assert response.status_code==200,response.text
    sid=response.json()['id']
    review=client.post(f'/api/editor/submissions/{sid}/review',headers=e,json={'accepted':True})
    assert review.status_code==200
    assert review.json()['order_completed'] is True
    assert review.json()['new_draft_version']>=2
    assert client.get(f'/api/events/{event}').status_code==404 # in private editor review again
    editor_state=client.get('/api/editor/events',headers=e).json()
    edited=next(x for x in editor_state['events'] if x['id']==event)
    assert edited['status']=='review'
    assert any(d['event_id']==event and d['version']>=2 for d in editor_state['drafts'])
    me=client.get('/api/auth/me',headers=u).json()
    assert me['credits']==0 and me['pending_points']==10 # баллы приходят на следующий день
    make_rewards_due()
    assert client.get('/api/auth/me',headers=u).json()['credits']==10
    assert client.post(f'/api/editor/events/{event}/decision',headers=e,json={'action':'publish','note':''}).status_code==422
    assert client.post(f'/api/editor/events/{event}/decision',headers=e,json={'action':'publish','note':'Данные проверено редактором по независимым источникам'}).status_code==200
    assert client.get(f'/api/events/{event}').json()['status']=='published'
    assert len(client.get(f'/api/events/{event}/history',headers=e).json()['analyses'])>=1


def test_phone_rate_limit_and_auth():
    assert client.post('/api/auth/request-code',json={'phone':'1234567'}).status_code==422
    u=make_user('+79990000003')
    assert client.get('/api/my/submissions',headers=u).status_code==200
    assert client.get('/api/my/submissions').status_code==401
    assert client.post('/api/submissions',headers=u,json={'kind':'refute','text':'У меня есть материал без события'}).status_code==422


def test_upload_and_private_media():
    from PIL import Image
    import io
    u=make_user('+79990000004')
    other=make_user('+79990000005')
    tip=client.post('/api/submissions',headers=u,json={'title':'Локальное событие у станции','text':'По сообщению организаторов открылась новая выставка'})
    sid=tip.json()['id']
    im=Image.new('RGB',(30,30),(40,150,190)); buf=io.BytesIO();im.save(buf,'JPEG')
    r=client.post(f'/api/submissions/{sid}/media',headers=u,files={'file':('test.jpg',buf.getvalue(),'image/jpeg')})
    assert r.status_code==200,r.text
    m=r.json()
    assert m['metadata_indicators']['gps_present'] is False
    assert client.get(f'/api/media/{m["id"]}',headers=other).status_code==403
    assert client.get(f'/api/media/{m["id"]}',headers=u).status_code==200
    assert client.post(f'/api/submissions/{sid}/media',headers=u,files={'file':('hack.jpg',b'not a real jpeg','image/jpeg')}).status_code==415


def test_external_integrations_secrets():
    payload={'provider':'telegram','provider_user_id':'12345','text':'Новости из доступного публичного сообщения'}
    assert client.post('/api/integrations/ingest',json=payload).status_code==403
    r=client.post('/api/integrations/ingest',headers={'X-Ingest-Key':'test-secret-ingest'},json=payload)
    assert r.status_code==200,r.text
    assert r.json()['status']=='review'
    wrong=client.post('/hooks/max',json={'update_type':'message_created','message':{'body':{'text':'Новое городское событие'},'sender':{'user_id':12}}})
    assert wrong.status_code==403
    accepted=client.post('/hooks/max',headers={'X-Max-Bot-Api-Secret':'test-max-secret'},json={'update_type':'message_created','message':{'body':{'text':'Новое городское событие'},'sender':{'user_id':12}}})
    assert accepted.status_code==200,accepted.text
    confirmation=client.post('/hooks/vk',json={'type':'confirmation','secret':'test-vk-secret'})
    assert confirmation.status_code==200
    assert confirmation.json()=='test-vk-confirm'


def test_publish_is_final_but_reject_can_be_published():
    user=make_user('+79990000006')
    editor=make_editor()
    def new_event(title):
        tip=client.post('/api/submissions',headers=user,json={'title':title,'text':'Материал для тестирования редакторских решений','kind':'tip'})
        assert tip.status_code==200,tip.text
        return f"/api/editor/events/{tip.json()['event_id']}/decision"
    approve={'action':'publish','note':'проверено редактором по независимым источникам'}
    revise={'action':'revise','question':'Нужны независимые доказательства'}
    # Сначала «Опубликовать»: дальше ничего изменить нельзя.
    url=new_event('Публикация окончательна')
    assert client.post(url,headers=editor,json=approve).status_code==200
    for payload in (approve,{'action':'reject'},revise):
        assert client.post(url,headers=editor,json=payload).status_code==409
    # Сначала «Отклонить»: опубликовать всё ещё можно, повтор отклонения — нет.
    url=new_event('Отклонено, затем опубликовано')
    assert client.post(url,headers=editor,json={'action':'reject'}).status_code==200
    assert client.post(url,headers=editor,json={'action':'reject'}).status_code==409
    assert client.post(url,headers=editor,json=approve).status_code==200
    assert client.post(url,headers=editor,json={'action':'reject'}).status_code==409



def test_editor_can_delete_news_without_losing_reward_history():
    user = make_user('+79990000019')
    editor = make_editor()
    created = client.post('/api/submissions', headers=user, json={
        'title': 'Новость для удаления',
        'text': 'Проверяем удаление новости из редакции и публичной ленты',
        'kind': 'tip',
    })
    assert created.status_code == 200, created.text
    event_id = created.json()['event_id']
    api_url = f'/api/editor/events/{event_id}'
    assert client.delete(api_url, headers=user).status_code == 403
    assert client.delete(api_url).status_code == 401
    assert any(e['id'] == event_id for e in client.get('/api/editor/events', headers=editor).json()['events'])
    assert client.delete(api_url, headers=editor).json() == {'deleted': True, 'event_id': event_id}
    assert client.delete(api_url, headers=editor).status_code == 404
    assert not any(e['id'] == event_id for e in client.get('/api/editor/events', headers=editor).json()['events'])
    assert not any(e['id'] == event_id for e in client.get('/api/events').json()['items'])
    assert client.get(f'/api/events/{event_id}').status_code == 404
    assert not any(s['event_id'] == event_id for s in client.get('/api/my/submissions', headers=user).json()['items'])
    assert client.post(f'{api_url}/decision', headers=editor, json={'action':'publish','note':'проверено редактором'}).status_code == 404
    assert client.post('/api/submissions', headers=user, json={
        'event_id': event_id, 'kind': 'confirm', 'text': 'Повторное подтверждение удалённой новости'
    }).status_code == 404
    from app.main import db
    with db() as con:
        assert con.execute('SELECT status FROM events WHERE id=?', (event_id,)).fetchone()['status'] == 'deleted'
        assert con.execute('SELECT COUNT(*) FROM submissions WHERE event_id=?', (event_id,)).fetchone()[0] == 1


def test_editor_delete_published_news_keeps_earned_rewards():
    user = make_user('+79990000020')
    editor = make_editor()
    created = client.post('/api/submissions', headers=user, json={
        'title': 'Опубликованная новость для удаления',
        'text': 'Редактор удаляет публикацию, начисленные баллы сохраняются',
        'kind': 'tip',
    })
    event_id = created.json()['event_id']
    assert client.post(f'/api/editor/events/{event_id}/decision', headers=editor,
        json={'action': 'publish', 'note': 'проверено редактором'}).status_code == 200
    before = client.get('/api/auth/me', headers=user).json()
    assert before['pending_points'] > 0
    assert client.delete(f'/api/editor/events/{event_id}', headers=editor).status_code == 200
    after = client.get('/api/auth/me', headers=user).json()
    assert (before['pending_points'], before['pending_money']) == (after['pending_points'], after['pending_money'])



def test_editor_can_delete_user_answers():
    user = make_user('+79990000021')
    editor = make_editor()
    created = client.post('/api/submissions', headers=user, json={
        'title': 'Новость с удаляемыми ответами',
        'text': 'Проверяем удаление пользовательских подтверждений',
        'kind': 'tip',
    })
    assert created.status_code == 200, created.text
    eid = created.json()['event_id']
    assert client.post(f'/api/editor/events/{eid}/decision', headers=editor, json={
        'action': 'revise', 'question': 'Нужны подтверждения'
    }).status_code == 200
    answer = client.post('/api/submissions', headers=user, json={
        'event_id': eid, 'kind': 'confirm', 'text': 'Подтверждаю уже известные сведения'
    })
    assert answer.status_code == 200, answer.text
    sid = answer.json()['id']
    url = f'/api/editor/submissions/{sid}'
    assert client.delete(url).status_code == 401
    assert client.delete(url, headers=user).status_code == 403
    assert any(s['id'] == sid for s in client.get('/api/editor/events', headers=editor).json()['submissions'])
    assert client.delete(url, headers=editor).json() == {'deleted': True, 'submission_id': sid}
    assert client.delete(url, headers=editor).status_code == 404
    state = client.get('/api/editor/events', headers=editor).json()
    assert not any(s['id'] == sid for s in state['submissions'])
    event = next(e for e in state['events'] if e['id'] == eid)
    assert event['submission_count'] == 1  # исходное сообщение осталось
    assert client.post(url + '/review', headers=editor, json={'accepted': True}).status_code == 404
    assert not any(s['id'] == sid for s in client.get('/api/my/submissions', headers=user).json()['items'])
    assert not any(s['id'] == sid for s in client.get(f'/api/events/{eid}/history', headers=editor).json()['submissions'])
    assert client.delete(f'/api/editor/submissions/{created.json()["id"]}', headers=editor).status_code == 409


def test_delete_reviewed_answer_preserves_rewards():
    user = make_user('+79990000022')
    editor = make_editor()
    created = client.post('/api/submissions', headers=user, json={
        'title': 'Событие для подтверждения',
        'text': 'Проверяем сохранность вознаграждений при удалении',
    })
    eid = created.json()['event_id']
    assert client.post(f'/api/editor/events/{eid}/decision', headers=editor, json={
        'action': 'revise', 'question': 'Нужны подтверждения'
    }).status_code == 200
    answer = client.post('/api/submissions', headers=user, json={
        'event_id': eid, 'kind': 'refute', 'text': 'Уже имеющиеся сведения это опровергают'
    })
    sid = answer.json()['id']
    assert client.post(f'/api/editor/submissions/{sid}/review',
                       headers=editor, json={'accepted': True}).status_code == 200
    before = client.get('/api/auth/me', headers=user).json()
    assert before['pending_points'] > 0
    assert client.delete(f'/api/editor/submissions/{sid}', headers=editor).status_code == 200
    after = client.get('/api/auth/me', headers=user).json()
    assert (after['pending_points'], after['pending_money']) == (before['pending_points'], before['pending_money'])
    from app.main import db
    with db() as con:
        assert con.execute('SELECT status FROM submissions WHERE id=?', (sid,)).fetchone()['status'] == 'deleted'
        assert con.execute('SELECT COUNT(*) FROM rewards WHERE submission_id=?', (sid,)).fetchone()[0] > 0


def test_reputation_rewards_and_wallet():
    user=make_user('+79990000007'); editor=make_editor()
    rules=client.get('/api/config').json()['rewards']
    def tip(title):
        r=client.post('/api/submissions',headers=user,json={'title':title,'text':'Сообщение очевидца для расчёта вознаграждения','kind':'tip'})
        return f"/api/editor/events/{r.json()['event_id']}/decision"
    # Достоверная эксклюзивная новость: 10 + 0,45·90 + 0,45·90 = 91 балл, тип «Эксклюзив».
    ok=client.post(tip('Эксклюзивная достоверная новость'),headers=editor,json={'action':'publish','note':'проверено редактором','importance':90,'exclusive_score':90})
    assert ok.status_code==200,ok.text
    me=client.get('/api/auth/me',headers=user).json()
    assert (me['credits'],me['wallet'],me['pending_points'],me['pending_money'])==(0,0,91,1500)
    assert client.post('/api/my/withdraw',headers=user).status_code==409
    # Недостоверная новость: только минус баллы, без денег.
    assert client.post(tip('Недостоверная новость без подтверждений'),headers=editor,json={'action':'reject'}).status_code==200
    make_rewards_due()
    me=client.get('/api/auth/me',headers=user).json()
    assert (me['credits'],me['wallet'],me['pending_points'])==(91+rules['false_news'],1500,0)
    items=client.get('/api/my/submissions',headers=user).json()['items']
    assert sorted(x['points'] for x in items)==[rules['false_news'],91]
    paid=client.post('/api/my/withdraw',headers=user)
    assert paid.status_code==200 and paid.json()['amount']==1500
    assert client.get('/api/auth/me',headers=user).json()['wallet']==0


def test_admin_phone_and_profile():
    from PIL import Image
    import io
    admin=make_user('+79999999999')
    me=client.get('/api/auth/me',headers=admin).json()
    assert me['role']=='admin' and me['nickname']=='Админ'
    assert client.get('/api/editor/events',headers=admin).status_code==200
    user=make_user('+79990000008')
    assert client.get('/api/auth/me',headers=user).json()['role']=='user'
    assert client.patch('/api/my/profile',headers=user,json={'nickname':'  Репортёр   Иван '}).json()['nickname']=='Репортёр Иван'
    assert client.patch('/api/my/profile',headers=user,json={'nickname':'x'}).status_code==422
    buf=io.BytesIO(); Image.new('RGB',(400,300),(7,108,252)).save(buf,'PNG')
    avatar=client.post('/api/my/avatar',headers=user,files={'file':('a.png',buf.getvalue(),'image/png')})
    assert avatar.status_code==200,avatar.text
    assert client.get(avatar.json()['avatar']).headers['content-type']=='image/jpeg'
    assert client.post('/api/my/avatar',headers=user,files={'file':('a.png',b'not an image','image/png')}).status_code==415

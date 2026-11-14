"""Telegram bot adapter for NewsRadar MVP.

Run separately from API server: python -m app.bots.telegram_bot
Requires TELEGRAM_BOT_TOKEN and INGEST_SECRET in the environment.
Accepts text, photos and video, and submits them to editorial review.
Does NOT automatically publish and does not demand geolocation.
"""
import asyncio
import os
from urllib.parse import urljoin

import httpx

TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','')
INGEST=os.getenv('INGEST_SECRET','')
API=os.getenv('NEWSRADAR_API','http://127.0.0.1:8000').rstrip('/')

async def run():
    if not TOKEN or not INGEST:
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and INGEST_SECRET first')
    url=f'https://api.telegram.org/bot{TOKEN}/'
    file_url=f'https://api.telegram.org/file/bot{TOKEN}/'
    timeout=httpx.Timeout(35,connect=12)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async def tg(method,**fields):
            r=await client.post(url+method,json=fields)
            r.raise_for_status()
            data=r.json()
            if not data.get('ok'):raise RuntimeError(data)
            return data['result']
        async def send(chat_id,text):
            await tg('sendMessage',chat_id=chat_id,text=text[:4000])
        async def ingest(p):
            r=await client.post(API+'/api/integrations/ingest',json=p,headers={'X-Ingest-Key':INGEST})
            r.raise_for_status()
            return r.json()
        async def upload(sid,media_obj,media_type):
            info=await tg('getFile',file_id=media_obj['file_id'])
            if int(info.get('file_size',0))>12*1024*1024:
                raise ValueError('Размер медиа превышает 12 МБ')
            link=file_url+info['file_path']
            media_response=await client.get(link)
            media_response.raise_for_status()
            body=media_response.content
            if len(body)>12*1024*1024:raise ValueError('Размер медиа превышает 12 МБ')
            ext=info['file_path'].lower().split('.')[-1]
            mime={'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp','mp4':'video/mp4','webm':'video/webm'}.get(ext)
            if not mime:raise ValueError('Неподдерживаемый формат файла')
            r=await client.post(API+f'/api/integrations/media/{sid}',files={'file':('telegram.'+ext,body,mime)},headers={'X-Ingest-Key':INGEST})
            r.raise_for_status()
        async def handle(update):
            msg=update.get('message',{})
            if not msg:return
            chat_id=msg['chat']['id']
            uid=str(msg['from']['id']) if 'from' in msg else str(chat_id)
            t=(msg.get('text') or msg.get('caption') or '').strip()
            if t.startswith('/start') or t.startswith('/help'):
                await send(chat_id,'NewsRadar MVP. Пришлите описание новости, фото или видео — передадим в редакторскую очередь. Для подтверждений и опровержений используйте веб-приложение. Не отправляйтесь на место происшествия ради материалов. /nearby — список открытых запросов на проверку.')
                return
            if t.startswith('/nearby'):
                r=await client.get(API+'/api/events',params={'status':'order'})
                r.raise_for_status()
                items=r.json()['items'][:8]
                await send(chat_id, 'Открытые запросы (местоположение не передавалось):\n' + ('\n'.join(f"#{x['id']}: {x['title']}" for x in items) if items else 'Нет открытых запросов'))
                return
            if msg.get('location') and not t:
                await send(chat_id,'Геолокация не сохраняется без описания сообщения. При необходимости укажите место события текстом.')
                return
            photo=msg.get('photo')
            media_obj=photo[-1] if photo else msg.get('video') or msg.get('document')
            if not t and not media_obj:return
            if not t:t='Пользователь прислал медиафайл без описания, требуется уточнение'
            try:
                p={'provider':'telegram','provider_user_id':uid,'text':t}
                if msg.get('location'):
                    p['latitude']=msg['location']['latitude'];p['longitude']=msg['location']['longitude']
                created=await ingest(p)
                if media_obj:
                    await upload(created['submission_id'],media_obj,'photo' if photo else 'video')
                await send(chat_id,f"✅ Сообщение #{created['event_id']} отправлено в очередь редактора. Публикация только после проверки.")
            except Exception as exc:
                print('Telegram processing error:',repr(exc),flush=True)
                await send(chat_id,'Не удалось обработать сообщение. Проверьте размер/формат файла и повторите попытку позже.')
        offset=None
        print('NewsRadar Telegram bot started',flush=True)
        while True:
            try:
                updates=await tg('getUpdates',offset=offset,timeout=25,allowed_updates=['message'])
                for u in updates:
                    offset=u['update_id']+1
                    await handle(u)
            except asyncio.CancelledError:raise
            except Exception as exc:
                print('Telegram connection error:',repr(exc),flush=True)
                await asyncio.sleep(3)

if __name__=='__main__':
    asyncio.run(run())

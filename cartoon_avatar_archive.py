"""Durable original-image links, including free admin previews; never generates images."""
import asyncio
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import quote, urlsplit

import aiosqlite

import database

SCHEMA='''CREATE TABLE IF NOT EXISTS cartoon_avatar_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    image_url TEXT NOT NULL, preview_url TEXT, theme TEXT NOT NULL,
    reference_url TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id,image_url))'''


def valid_url(value):
    if not isinstance(value,str) or len(value)>2000:return False
    p=urlsplit(value)
    return p.scheme=='https' and bool(p.hostname) and not p.username and not p.password


async def save(user_id,image_url,theme,*,preview_url=None,reference_url=None):
    if not valid_url(image_url):raise ValueError('Нет HTTPS-ссылки на оригинал.')
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute(SCHEMA)
        await db.execute('''INSERT INTO cartoon_avatar_archive(user_id,image_url,preview_url,theme,reference_url)
            VALUES(?,?,?,?,?) ON CONFLICT(user_id,image_url) DO UPDATE SET
            preview_url=COALESCE(excluded.preview_url,preview_url)''',
            (user_id,image_url,preview_url,theme,reference_url))
        row=await (await db.execute('SELECT id FROM cartoon_avatar_archive WHERE user_id=? AND image_url=?',
            (user_id,image_url))).fetchone()
        await db.commit()
        return row[0]


async def get(user_id,asset_id):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute(SCHEMA);db.row_factory=aiosqlite.Row
        row=await (await db.execute('SELECT * FROM cartoon_avatar_archive WHERE user_id=? AND id=?',
            (user_id,asset_id))).fetchone()
        return dict(row) if row else None


async def recent(user_id,limit=10):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute(SCHEMA);db.row_factory=aiosqlite.Row
        rows=await (await db.execute('SELECT * FROM cartoon_avatar_archive WHERE user_id=? ORDER BY id DESC LIMIT ?',
            (user_id,limit))).fetchall()
        return [dict(row) for row in rows]


def originals(data):
    """Known clean fields only. A PREVIEW URL is never promoted to an original."""
    values=[data.get('chosen_avatar'),data.get('original_image_url')]
    values+=list((data.get('avatar_urls') or {}).values())
    values+=[data.get('avatar_1_url'),data.get('avatar_2_url')]
    previews=set((data.get('avatar_preview_urls') or {}).values())
    previews.add(data.get('watermarked_image_url'))
    return list(dict.fromkeys(v for v in values if valid_url(v) and v not in previews))


async def remember(user_id,data):
    if not data.get('cartoon_theme'):return
    for url in originals(data):
        await save(user_id,url,data['cartoon_theme'],reference_url=data.get('avatar_reference_url'))


async def import_saved_orders(user_id):
    import json
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        rows=await (await db.execute('SELECT theme,fsm_data FROM cartoon_orders WHERE user_id=? '
            'AND fsm_data IS NOT NULL ORDER BY rowid DESC LIMIT 30',(user_id,))).fetchall()
    for theme,raw in rows:
        try:data=json.loads(raw)
        except (TypeError,ValueError):continue
        if not isinstance(data,dict):continue
        data.setdefault('cartoon_theme',theme)
        await remember(user_id,data)


def display_date(value):
    from zoneinfo import ZoneInfo
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')


def storage_base(config):
    if not config.storage_bucket:raise ValueError('Не настроено хранилище изображений.')
    return (config.storage_public_base_url or
        'https://storage.yandexcloud.net/'+quote(config.storage_bucket,safe='')).rstrip('/')


def storage_key(config,url):
    base=storage_base(config)
    if not isinstance(url,str) or not url.startswith(base+'/'):raise ValueError('Нужна ссылка из хранилища этого бота.')
    key=url[len(base)+1:]
    if not re.fullmatch(r'vision-boards/[0-9a-f]{32}\.png',key):
        raise ValueError('Нужна исходная PNG-картинка из папки vision-boards.')
    return key


def _legacy_objects(config):
    """Read-only, bounded scan. UUID keys cannot be sorted by generation date."""
    from image_service import _storage_client
    client=_storage_client(config)
    try:
        items=[];token=None;complete=False
        since=datetime.now(timezone.utc)-timedelta(days=7)
        for _ in range(20):
            args={'Bucket':config.storage_bucket,'Prefix':'vision-boards/','MaxKeys':1000}
            if token:args['ContinuationToken']=token
            page=client.list_objects_v2(**args)
            for obj in page.get('Contents',[]):
                if re.fullmatch(r'vision-boards/[0-9a-f]{32}\.png',obj['Key']) and obj['LastModified']>=since:
                    items.append(obj)
            if not page.get('IsTruncated'):complete=True;break
            new_token=page.get('NextContinuationToken')
            if not new_token or new_token==token:break
            token=new_token
        items.sort(key=lambda x:x['LastModified'],reverse=True)
        return [{'url':storage_base(config)+'/'+o['Key'],'date':o['LastModified'].isoformat()} for o in items[:60]],complete
    finally:client.close()


async def legacy_objects(config):
    return await asyncio.to_thread(_legacy_objects,config)

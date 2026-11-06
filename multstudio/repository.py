"""Only cartoon_* tables. Immutable revisions and transactional budget reservations."""
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import aiosqlite
import database

logger = logging.getLogger(__name__)
RESTORE_NOT_FOUND = 'Не удалось найти сохраненные файлы для этой сцены в архиве заданий'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    # Publishing an immutable file must not change scene identity or stale a quote.
    def stable(item):
        if isinstance(item, dict):
            technical={'public_url','path','metadata','source_url','created_at'} if {'id','sha256','path'}<=item.keys() else {'public_url'}
            return {k:stable(v) for k,v in item.items() if k not in technical}
        if isinstance(item, list):
            return [stable(v) for v in item]
        return item
    return hashlib.sha256(encoded(stable(value)).encode()).hexdigest()


@asynccontextmanager
async def connect():
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('PRAGMA foreign_keys=ON')
        yield db


async def one(db, sql, args=()):
    async with db.execute(sql, args) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def rows(db, sql, args=()):
    async with db.execute(sql, args) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def init():
    async with connect() as db:
        await db.executescript('''
CREATE TABLE IF NOT EXISTS cartoon_projects (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, title TEXT NOT NULL, season INTEGER NOT NULL,
 spec TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, budget INTEGER NOT NULL DEFAULT 0,
 seed_key TEXT UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS cartoon_characters (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES cartoon_projects(id), name TEXT NOT NULL,
 spec TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, UNIQUE(project_id,name));
CREATE TABLE IF NOT EXISTS cartoon_episodes (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES cartoon_projects(id), title TEXT NOT NULL,
 number INTEGER NOT NULL, spec TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, UNIQUE(project_id,number));
CREATE TABLE IF NOT EXISTS cartoon_scenes (
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES cartoon_episodes(id), number INTEGER NOT NULL,
 spec TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, UNIQUE(episode_id,number));
CREATE TABLE IF NOT EXISTS cartoon_versions (
 entity TEXT NOT NULL, entity_id TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(entity,entity_id,revision));
CREATE TABLE IF NOT EXISTS cartoon_jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES cartoon_projects(id), target TEXT NOT NULL,
 target_id TEXT NOT NULL, kind TEXT NOT NULL, snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'quoted', estimate INTEGER NOT NULL, actual INTEGER,
 usage TEXT, approved_by INTEGER, expires INTEGER NOT NULL, provider_id TEXT, result_url TEXT,
 lease_until INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0,
 error TEXT, notified INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS cartoon_assets (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES cartoon_projects(id), target TEXT NOT NULL,
 target_id TEXT NOT NULL, kind TEXT NOT NULL, context_hash TEXT NOT NULL, path TEXT NOT NULL,
 sha256 TEXT NOT NULL, public_url TEXT, source_url TEXT, approved INTEGER NOT NULL DEFAULT 0,
 job_id TEXT REFERENCES cartoon_jobs(id), metadata TEXT NOT NULL DEFAULT '{}',
 created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS cartoon_job_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES cartoon_jobs(id),
 event TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS cartoon_scene_image_selections (
 scene_id TEXT PRIMARY KEY REFERENCES cartoon_scenes(id), context_hash TEXT NOT NULL,
 asset_id TEXT NOT NULL REFERENCES cartoon_assets(id));
CREATE TABLE IF NOT EXISTS cartoon_scene_asset_restorations (
 scene_id TEXT NOT NULL REFERENCES cartoon_scenes(id), context_hash TEXT NOT NULL,
 kind TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES cartoon_assets(id),
 asset_watermark INTEGER NOT NULL, PRIMARY KEY(scene_id,context_hash,kind));
CREATE TABLE IF NOT EXISTS cartoon_character_speech_profiles (
 character_id TEXT PRIMARY KEY REFERENCES cartoon_characters(id), profile TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cartoon_scene_editing (
 scene_id TEXT PRIMARY KEY REFERENCES cartoon_scenes(id), speech_start REAL NOT NULL,
 video_start REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cartoon_editing_migrations (
 name TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES cartoon_projects(id));
CREATE INDEX IF NOT EXISTS cartoon_jobs_queue ON cartoon_jobs(status,next_attempt,lease_until);
''')
        columns = await rows(db,'PRAGMA table_info(cartoon_jobs)')
        if 'result_url' not in {column['name'] for column in columns}:
            await db.execute('ALTER TABLE cartoon_jobs ADD COLUMN result_url TEXT')
        editing_columns=await rows(db,'PRAGMA table_info(cartoon_scene_editing)')
        if 'video_start' not in {column['name'] for column in editing_columns}:
            await db.execute('ALTER TABLE cartoon_scene_editing ADD COLUMN video_start REAL NOT NULL DEFAULT 0')
        await _apply_episode_timings(db)
        await _ensure_masha_voice(db)
        await db.commit()


TABLES = {'project': 'cartoon_projects', 'character': 'cartoon_characters',
          'episode': 'cartoon_episodes', 'scene': 'cartoon_scenes'}


def unpack(row):
    if row:
        row['spec'] = json.loads(row['spec'])
    return row


async def get(entity, key):
    async with connect() as db:
        return unpack(await one(db, f'SELECT * FROM {TABLES[entity]} WHERE id=?', (key,)))


async def children(entity, parent=None):
    table = TABLES[entity]
    parent_field = {'character': 'project_id', 'episode': 'project_id', 'scene': 'episode_id'}.get(entity)
    async with connect() as db:
        result = await rows(db, f'SELECT * FROM {table}' + (f' WHERE {parent_field}=?' if parent_field else '')
                            + ' ORDER BY rowid', (parent,) if parent_field else ())
    return [unpack(r) for r in result]


async def _ensure_masha_voice(db):
    # Only the approved seed series. Never rewrite character specs/revisions or media hashes.
    from .tts import MASHA_PROFILE
    await db.execute('''INSERT OR IGNORE INTO cartoon_character_speech_profiles(character_id,profile)
        SELECT c.id,? FROM cartoon_characters c JOIN cartoon_projects p ON p.id=c.project_id
        WHERE p.seed_key='anya-s1-v1' AND c.name='Маша' ''', (encoded(MASHA_PROFILE),))


async def ensure_masha_voice():
    async with connect() as db:
        await _ensure_masha_voice(db)
        await db.commit()


async def character_speech(key):
    from .tts import speech_settings
    async with connect() as db:
        row=await one(db,'''SELECT c.name,c.spec,p.profile FROM cartoon_characters c
            LEFT JOIN cartoon_character_speech_profiles p ON p.character_id=c.id WHERE c.id=?''',(key,))
    if not row: raise ValueError('Персонаж не найден.')
    return speech_settings(json.loads(row['profile'] or row['spec']),row['name'])


async def set_character_speech(key,actor,profile):
    from .tts import speech_settings
    profile=speech_settings(profile)
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        row=await one(db,'''SELECT c.id FROM cartoon_characters c JOIN cartoon_projects p ON p.id=c.project_id
            WHERE c.id=? AND p.owner_id=?''',(key,actor))
        if not row: raise ValueError('Нет доступа к персонажу.')
        await db.execute('INSERT OR REPLACE INTO cartoon_character_speech_profiles VALUES (?,?)',(key,encoded(profile)))
        await db.commit()
    return profile


async def create(entity, spec, **fields):
    key = uuid4().hex
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        cols = ['id', 'spec', *fields]
        await db.execute(f'INSERT INTO {TABLES[entity]} ({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})',
                         (key, encoded(spec), *fields.values()))
        await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',
                         (entity, key, 1, encoded(dict(spec=spec, **fields))))
        await db.commit()
    return key


async def edit(entity, key, spec):
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        row = await one(db, f'SELECT * FROM {TABLES[entity]} WHERE id=?', (key,))
        if not row:
            raise ValueError('Запись не найдена.')
        revision = row['revision'] + 1
        await db.execute(f'UPDATE {TABLES[entity]} SET spec=?,revision=? WHERE id=?', (encoded(spec), revision, key))
        await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)', (entity, key, revision, encoded(spec)))
        await db.commit()


async def update_character_description(key, actor, description):
    """Patch only description from current stored spec, atomically with its revision."""
    if not isinstance(description,str) or not description.strip() or len(description)>10000:
        raise ValueError('Описание должно содержать от 1 до 10000 символов обычного текста.')
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        row=await one(db,'''SELECT c.*,p.owner_id FROM cartoon_characters c
            JOIN cartoon_projects p ON p.id=c.project_id WHERE c.id=?''',(key,))
        if not row or row['owner_id']!=actor:
            raise ValueError('Персонаж недоступен или нет доступа к проекту.')
        try:
            spec=json.loads(row['spec']) if isinstance(row['spec'],str) else row['spec']
        except json.JSONDecodeError:
            raise ValueError('Настройки персонажа в базе повреждены. Изменений нет.') from None
        if not isinstance(spec,dict):
            raise ValueError('Настройки персонажа в базе повреждены. Изменений нет.')
        if spec.get('description')==description:
            return
        spec=dict(spec,description=description)
        revision=row['revision']+1
        await db.execute('UPDATE cartoon_characters SET spec=?,revision=? WHERE id=?',(encoded(spec),revision,key))
        await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',
                         ('character',key,revision,encoded(spec)))
        await db.commit()


async def update_scene_visual(key, actor, visual):
    """Patch the current visual only; preserve dialogue, cast, camera and timing."""
    if not isinstance(visual, str) or not visual.strip() or len(visual) > 10000:
        raise ValueError('Описание сцены должно содержать от 1 до 10000 символов обычного текста.')
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        row = await one(db, '''SELECT s.*,p.owner_id FROM cartoon_scenes s
            JOIN cartoon_episodes e ON e.id=s.episode_id
            JOIN cartoon_projects p ON p.id=e.project_id WHERE s.id=?''', (key,))
        if not row or row['owner_id'] != actor:
            raise ValueError('Сцена недоступна или нет доступа к проекту.')
        try:
            spec = json.loads(row['spec']) if isinstance(row['spec'], str) else row['spec']
        except json.JSONDecodeError:
            raise ValueError('Настройки сцены в базе повреждены. Изменений нет.') from None
        if not isinstance(spec, dict):
            raise ValueError('Настройки сцены в базе повреждены. Изменений нет.')
        if spec.get('visual') == visual:
            return
        spec = dict(spec, visual=visual)
        revision = row['revision'] + 1
        await db.execute('UPDATE cartoon_scenes SET spec=?,revision=? WHERE id=?', (encoded(spec), revision, key))
        await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',
                         ('scene', key, revision, encoded(spec)))
        await db.commit()


async def liability(db, project):
    row = await one(db, '''SELECT COALESCE(SUM(CASE WHEN actual IS NOT NULL THEN actual ELSE estimate END),0) AS total
      FROM cartoon_jobs WHERE project_id=? AND status NOT IN ('quoted','cancelled','stale','preflight_failed')''', (project,))
    return row['total']


async def budget(project, amount=None):
    async with connect() as db:
        if amount is not None:
            if not isinstance(amount, int) or not 0 <= amount <= 100000000000:
                raise ValueError('Некорректный бюджет.')
            await db.execute('UPDATE cartoon_projects SET budget=? WHERE id=?', (amount, project))
            await db.commit()
        row = await one(db, 'SELECT budget FROM cartoon_projects WHERE id=?', (project,))
        return dict(limit=row['budget'], committed=await liability(db, project))


async def _latest(db, target, key, kind, context):
    if kind=='image':
        return await one(db, '''SELECT * FROM cartoon_assets WHERE target=? AND target_id=?
          AND kind IN ('image','character_reference') AND context_hash=? ORDER BY rowid DESC LIMIT 1''', (target,key,context))
    return await one(db, '''SELECT * FROM cartoon_assets WHERE target=? AND target_id=? AND kind=? AND context_hash=?
                         ORDER BY rowid DESC LIMIT 1''', (target, key, kind, context))


def speech_start(value):
    import math
    try:
        if isinstance(value,bool): raise ValueError
        seconds=float(str(value).replace(',', '.'))
        if not math.isfinite(seconds) or not 0<=seconds<=30: raise ValueError
    except (ValueError,TypeError,OverflowError):
        raise ValueError('Начало речи: число от 0 до 30 секунд, например 3 или 1.5.') from None
    return seconds


async def _scene_editing(db,key):
    row=await one(db,'''SELECT s.number,e.number AS episode_number,p.seed_key,t.speech_start,t.video_start FROM cartoon_scenes s
        JOIN cartoon_episodes e ON e.id=s.episode_id JOIN cartoon_projects p ON p.id=e.project_id
        LEFT JOIN cartoon_scene_editing t ON t.scene_id=s.id WHERE s.id=?''',(key,))
    if not row: raise ValueError('Сцена не найдена.')
    default=1.0 if row['seed_key']=='anya-s1-v1' and row['episode_number']==1 and row['number'] in (1,5) else 0.0
    return dict(speech_start=row['speech_start'] if row['speech_start'] is not None else default,
                video_start=row['video_start'] if row['video_start'] is not None else 0.0)


async def scene_editing(key):
    async with connect() as db:
        return await _scene_editing(db,key)


async def set_scene_speech_start(key,actor,value):
    value=speech_start(value)
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        row=await one(db,'''SELECT s.id FROM cartoon_scenes s JOIN cartoon_episodes e ON e.id=s.episode_id
            JOIN cartoon_projects p ON p.id=e.project_id WHERE s.id=? AND p.owner_id=?''',(key,actor))
        if not row: raise ValueError('Нет доступа к сцене.')
        await db.execute('''INSERT INTO cartoon_scene_editing(scene_id,speech_start) VALUES (?,?)
            ON CONFLICT(scene_id) DO UPDATE SET speech_start=excluded.speech_start''',(key,value))
        await db.commit()
    return value


def video_start(value):
    try:
        return speech_start(value)
    except ValueError:
        raise ValueError('Начало видео: число от 0 до 30 секунд исходного ролика.') from None


async def set_scene_video_start(key,actor,value):
    value=video_start(value)
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        row=await one(db,'''SELECT s.id FROM cartoon_scenes s JOIN cartoon_episodes e ON e.id=s.episode_id
            JOIN cartoon_projects p ON p.id=e.project_id WHERE s.id=? AND p.owner_id=?''',(key,actor))
        if not row: raise ValueError('Нет доступа к сцене.')
        current=await _scene_editing(db,key)
        await db.execute('''INSERT INTO cartoon_scene_editing(scene_id,speech_start,video_start) VALUES (?,?,?)
            ON CONFLICT(scene_id) DO UPDATE SET video_start=excluded.video_start''',
            (key,current['speech_start'],value))
        await db.commit()
    return value


async def _apply_episode_timings(db):
    name='anya-s1-e1-timing-20261106-v2'
    if await one(db,'SELECT name FROM cartoon_editing_migrations WHERE name=?',(name,)):
        return
    scenes=await rows(db,'''SELECT s.id,s.number,p.id AS project_id FROM cartoon_scenes s
        JOIN cartoon_episodes e ON e.id=s.episode_id JOIN cartoon_projects p ON p.id=e.project_id
        WHERE p.seed_key='anya-s1-v1' AND e.number=1 AND s.number IN (1,2,4,5)''')
    if len(scenes)!=4: return
    for scene in scenes:
        offset=1.0 if scene['number'] in (1,5) else 0.0
        cut=4.0 if scene['number']==2 else 0.0
        await db.execute('''INSERT INTO cartoon_scene_editing(scene_id,speech_start,video_start) VALUES (?,?,?)
            ON CONFLICT(scene_id) DO UPDATE SET speech_start=excluded.speech_start,
            video_start=CASE WHEN ? THEN excluded.video_start ELSE cartoon_scene_editing.video_start END''',
            (scene['id'],offset,cut,scene['number']==2))
    await db.execute('INSERT INTO cartoon_editing_migrations VALUES (?,?)',(name,scenes[0]['project_id']))


async def ensure_episode_timings():
    async with connect() as db:
        await _apply_episode_timings(db)
        await db.commit()


async def _snapshot(db, target, key):
    row = unpack(await one(db, f'SELECT * FROM {TABLES[target]} WHERE id=?', (key,)))
    if not row:
        raise ValueError('Запись не найдена.')
    if target == 'scene':
        episode = unpack(await one(db, 'SELECT * FROM cartoon_episodes WHERE id=?', (row['episode_id'],)))
        project_id = episode['project_id']
    else:
        project_id = row['project_id']
    project = unpack(await one(db, 'SELECT * FROM cartoon_projects WHERE id=?', (project_id,)))
    snap = dict(target=target, id=key, project_id=project_id, revision=row['revision'], spec=row['spec'],
                world=project['spec'], world_revision=project['revision'])
    if target == 'episode':
        scene_rows = await rows(db, 'SELECT id FROM cartoon_scenes WHERE episode_id=? ORDER BY number', (key,))
        snap['scenes'] = [await _snapshot(db, 'scene', s['id']) for s in scene_rows]
        # Editing is attached only to episode snapshots, after each scene's visual hash.
        for scene in snap['scenes']:
            scene['editing']=await _scene_editing(db,scene['id'])
    elif target == 'scene':
        snap['number'] = row['number']
        cast = []
        for character_id in row['spec'].get('characters', []):
            char = await _snapshot(db, 'character', character_id)
            if char['project_id'] != project_id:
                raise ValueError('Персонаж из другого проекта.')
            cast.append(char)
        snap['cast'] = cast
        if row['number'] > 1:
            from .locations import use_previous_location
            previous = unpack(await one(db, 'SELECT * FROM cartoon_scenes WHERE episode_id=? AND number=?',
                                        (row['episode_id'], row['number'] - 1)))
            snap['location_reference_required'] = not previous or use_previous_location(previous['spec'], row['spec'])
            if previous and snap['location_reference_required']:
                previous_snap = await _snapshot(db, 'scene', previous['id'])
                location = previous_snap['assets'].get('image')
                if location and location['approved']:
                    snap['location_reference'] = location
    if target == 'episode':
        snap['music'] = await one(db, "SELECT * FROM cartoon_assets WHERE target='episode' AND target_id=? AND kind='music' ORDER BY rowid DESC LIMIT 1", (key,))
    snap['context_hash'] = digest(snap)
    snap['assets'] = {}
    for kind in ('image', 'video', 'speech', 'assemble', 'music', 'ending'):
        asset = await _latest(db, target, key, kind, snap['context_hash'])
        if asset:
            snap['assets'][kind] = asset
    if target == 'scene':
        selected = await one(db, '''SELECT a.* FROM cartoon_scene_image_selections s
            JOIN cartoon_assets a ON a.id=s.asset_id WHERE s.scene_id=? AND s.context_hash=?
            AND a.context_hash=? AND a.approved=1 AND a.kind='image' ''',
            (key,snap['context_hash'],snap['context_hash']))
        if selected:
            snap['assets']['image'] = selected
        restored = await rows(db, '''SELECT a.* FROM cartoon_scene_asset_restorations r
            JOIN cartoon_assets a ON a.id=r.asset_id
            WHERE r.scene_id=? AND r.context_hash=? AND a.project_id=?
              AND a.target='scene' AND a.target_id=r.scene_id AND a.kind=r.kind
              AND NOT EXISTS (SELECT 1 FROM cartoon_assets n WHERE n.target='scene'
                AND n.target_id=r.scene_id AND n.project_id=a.project_id AND n.kind=r.kind
                AND n.context_hash=r.context_hash AND n.rowid>r.asset_watermark)''',
            (key,snap['context_hash'],project_id))
        for asset in restored:
            snap['assets'][asset['kind']] = asset
    if snap.get('music'):
        snap['assets']['music'] = snap['music']
    if target == 'character' and row['spec'].get('reference'):
        asset = await one(db, 'SELECT * FROM cartoon_assets WHERE id=?', (row['spec']['reference'],))
        if not asset or asset['project_id'] != project_id or asset['kind'] not in ('image','character_reference'):
            raise ValueError('Не найден сохранённый референс персонажа.')
        snap['assets']['image'] = asset
    return snap


async def snapshot(target, key):
    async with connect() as db:
        return await _snapshot(db, target, key)


async def restore_scene_assets(scene_id, *, actor=None, assets=None):
    """Explicitly select archived results; retain original asset IDs/context/history."""
    publish = assets is not None
    if assets is None:
        import os
        from pathlib import Path
        from .assets import Assets
        assets = Assets(Path(os.getenv('STUDIO_FILES_DIR',str(Path(__file__).resolve().parents[1]/'studio_files'))),None)
    async with connect() as db:
        scene = await one(db, '''SELECT p.id AS project_id,p.owner_id FROM cartoon_scenes s
            JOIN cartoon_episodes e ON e.id=s.episode_id
            JOIN cartoon_projects p ON p.id=e.project_id WHERE s.id=?''',(scene_id,))
        if not scene or (actor is not None and actor!=scene['owner_id']):
            raise ValueError('Нет доступа к сцене.')
        before = await _snapshot(db,'scene',scene_id)
        jobs = await rows(db, '''SELECT * FROM cartoon_jobs WHERE target='scene' AND target_id=?
            AND project_id=? AND status='done' AND kind IN ('image','scene_image','video')
            ORDER BY rowid DESC''',(scene_id,scene['project_id']))
        candidates = {}
        for job in jobs:
            kind='video' if job['kind']=='video' else 'image'
            candidates[job['id']] = await rows(db, '''SELECT * FROM cartoon_assets
                WHERE job_id=? AND project_id=? AND kind=? ORDER BY rowid DESC''',
                (job['id'],scene['project_id'],kind))
        # A video snapshot retains its input image descriptor even if its asset row was lost.
        for video in (j for j in jobs if j['kind']=='video'):
            try:
                image=json.loads(video['snapshot']).get('assets',{}).get('image') or {}
            except (ValueError,TypeError,AttributeError):
                continue
            image_job=image.get('job_id')
            if image_job in candidates and not candidates[image_job] and image.get('target_id')==scene_id:
                candidates[image_job]=[image]
    found={}
    for job in jobs:
        kind='video' if job['kind']=='video' else 'image'
        if kind in found:
            continue
        result=await assets.archived_scene_file(job,kind,candidates[job['id']])
        if result is None:
            continue
        if publish:
            try:
                result['public_url']=await assets.publish(result['path'])
            except Exception as exc:
                logger.warning('Архив сцены: публикация недоступна scene=%s job=%s error=%s',scene_id,job['id'],type(exc).__name__)
                raise ValueError('Сохранённые файлы найдены, но публикация временно недоступна. '
                                 'Повторите восстановление; новая генерация не запускается.') from None
        found[kind]=(job,result)
    if not found:
        raise ValueError(RESTORE_NOT_FOUND)
    restored={}
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        current=await _snapshot(db,'scene',scene_id)
        if current['context_hash']!=before['context_hash']:
            raise ValueError('Сцена изменилась во время восстановления. Повторите действие.')
        for kind,(job,result) in found.items():
            existing=await one(db,'SELECT * FROM cartoon_assets WHERE id=?',(result.get('id'),))
            if existing and (existing['project_id']!=scene['project_id'] or existing['job_id']!=job['id']):
                raise ValueError('Архивный результат принадлежит другому заданию.')
            asset_id=existing['id'] if existing else result.get('id') or uuid4().hex
            try:
                metadata=json.loads(result.get('metadata') or '{}')
            except (ValueError,TypeError):
                metadata={}
            if not isinstance(metadata,dict):
                metadata={}
            if publish:
                metadata.update(published_key=assets.object_key(result['path']),published_sha256=result['sha256'])
            if existing:
                await db.execute("UPDATE cartoon_assets SET target='scene',target_id=?,path=?,sha256=?,public_url=?,metadata=? WHERE id=?",
                    (scene_id,result['path'],result['sha256'],result.get('public_url'),encoded(metadata),asset_id))
            else:
                await db.execute('''INSERT INTO cartoon_assets
                    (id,project_id,target,target_id,kind,context_hash,path,sha256,public_url,approved,job_id,metadata)
                    VALUES (?,?,'scene',?,?,?,?,?,?,?,?,?)''',
                    (asset_id,scene['project_id'],scene_id,kind,result.get('context_hash') or current['context_hash'],
                     result['path'],result['sha256'],result.get('public_url'),int(bool(result.get('approved'))),job['id'],encoded(metadata)))
            watermark=await one(db,"SELECT COALESCE(MAX(rowid),0) AS n FROM cartoon_assets WHERE target='scene' AND target_id=?",(scene_id,))
            prior=await one(db,'SELECT asset_id FROM cartoon_scene_asset_restorations WHERE scene_id=? AND context_hash=? AND kind=?',
                            (scene_id,current['context_hash'],kind))
            await db.execute('INSERT OR REPLACE INTO cartoon_scene_asset_restorations VALUES (?,?,?,?,?)',
                             (scene_id,current['context_hash'],kind,asset_id,watermark['n']))
            if not prior or prior['asset_id']!=asset_id:
                await db.execute('INSERT INTO cartoon_job_events(job_id,event,details) VALUES (?,?,?)',
                                 (job['id'],'scene_assets_restored',encoded(dict(scene_id=scene_id,asset_id=asset_id,kind=kind,actor=actor))))
            restored[kind]=asset_id
        await db.commit()
    logger.info('Восстановлены ассеты сцены %s: %s',scene_id,restored)
    return dict(success=True,restored=restored,missing=[kind for kind in ('image','video') if kind not in restored])


async def quote(target, key, kind, settings):
    if (target, kind) not in {('character','image'),('scene','image'),('scene','video'),('scene','speech'),('episode','assemble')}:
        raise ValueError('Недопустимый тип задания.')
    snap = await snapshot(target, key)
    if kind == 'image' and target == 'scene':
        if not snap['spec'].get('approved') or not snap.get('cast'):
            raise ValueError('Сначала утвердите сценарий и укажите персонажей.')
        if any(not c['assets'].get('image') or not c['assets']['image']['approved'] for c in snap['cast']):
            raise ValueError('Сначала создайте и утвердите эталоны всех персонажей сцены.')
        if snap.get('location_reference_required') and not snap.get('location_reference'):
            raise ValueError('Сначала создайте и утвердите изображение предыдущей сцены как эталон комнаты.')
    if kind in ('video','speech') and (not snap['assets'].get('image') or not snap['assets']['image']['approved']):
        raise ValueError('Сначала утвердите изображение этой версии сцены.')
    if kind == 'speech' and not snap['spec'].get('dialogue'):
        raise ValueError('В сцене нет реплик.')
    if kind == 'assemble':
        if not snap.get('scenes') or any(not (s['assets'].get('image') or s['assets'].get('video'))
             or (s['spec'].get('dialogue') and not s['assets'].get('speech')) for s in snap['scenes']):
            raise ValueError('Для каждой сцены нужны изображение/видео и озвучка всех реплик.')
    profiles = None
    if kind == 'speech':
        profiles = {}
        for character in snap['cast']:
            row = await get('character',character['id'])
            profiles[character['id']] = dict(await character_speech(character['id']),name=row['name'])
    cost = settings.quote(kind, snap, speech_profiles=profiles) if profiles is not None else settings.quote(kind, snap)
    if profiles is not None:
        cost['speech_profiles'] = profiles
    if kind=='assemble':
        cost['render_version']=3
    snap['quote'] = cost
    fingerprint = digest(snap)
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        prior = await one(db, '''SELECT * FROM cartoon_jobs WHERE target=? AND target_id=? AND kind=? AND fingerprint=?
            AND status IN ('quoted','queued','preparing','submitting','polling') AND expires>? ORDER BY rowid DESC LIMIT 1''',
                          (target, key, kind, fingerprint, int(time.time())))
        if prior:
            return prior
        job_id = uuid4().hex
        await db.execute('''INSERT INTO cartoon_jobs(id,project_id,target,target_id,kind,snapshot,fingerprint,estimate,expires)
          VALUES (?,?,?,?,?,?,?,?,?)''', (job_id, snap['project_id'], target, key, kind, encoded(snap), fingerprint,
                                        cost['estimate'], int(time.time()) + 1800))
        await db.commit()
    return await job(job_id)


async def job(key):
    async with connect() as db:
        return await one(db, 'SELECT * FROM cartoon_jobs WHERE id=?', (key,))


def is_image_job(item):
    return item['kind'] in ('image','character_reference','scene_image')


def is_media_job(item):
    return is_image_job(item) or item['kind'] in ('video','speech','assemble','episode_assembly')


async def begin_scene_upload(key, actor):
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        row = await one(db, '''SELECT p.owner_id FROM cartoon_scenes s
            JOIN cartoon_episodes e ON e.id=s.episode_id
            JOIN cartoon_projects p ON p.id=e.project_id WHERE s.id=?''', (key,))
        if not row or row['owner_id'] != actor:
            raise ValueError('Сцена недоступна или нет доступа к проекту.')
        snap = await _snapshot(db, 'scene', key)
        job_id = uuid4().hex
        await db.execute('''INSERT INTO cartoon_jobs
            (id,project_id,target,target_id,kind,snapshot,fingerprint,status,estimate,actual,approved_by,expires,notified)
            VALUES (?,?,'scene',?,'scene_image',?,?,'uploading',0,0,?,0,1)''',
            (job_id,snap['project_id'],key,encoded(snap),digest(snap),actor))
        await db.commit()
    return job_id, snap


async def finish_scene_upload(key, asset_id, url):
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        item = await one(db, "SELECT * FROM cartoon_jobs WHERE id=? AND kind='scene_image' AND status='uploading'", (key,))
        asset = await one(db, "SELECT * FROM cartoon_assets WHERE id=? AND job_id=? AND kind='scene_upload_pending'", (asset_id,key))
        if not item or not asset or asset['target_id'] != item['target_id']:
            raise ValueError('Не найден результат загрузки.')
        current = await _snapshot(db, 'scene', item['target_id'])
        if current['context_hash'] != json.loads(item['snapshot'])['context_hash']:
            raise ValueError('Сцена изменилась во время загрузки. Загрузите изображение для новой версии.')
        await db.execute("UPDATE cartoon_assets SET public_url=?,approved=1,kind='image' WHERE id=?", (url,asset_id))
        await db.execute('INSERT OR REPLACE INTO cartoon_scene_image_selections VALUES (?,?,?)',
                         (item['target_id'],current['context_hash'],asset_id))
        await db.execute("UPDATE cartoon_jobs SET status='done',result_url=?,actual=0 WHERE id=?", (url,key))
        await db.execute('INSERT INTO cartoon_job_events(job_id,event,details) VALUES (?,?,?)',
                         (key,'scene_image_uploaded',encoded({'asset_id':asset_id,'actual':0})))
        await db.commit()


async def job_result(key):
    async with connect() as db:
        item=await one(db,'SELECT * FROM cartoon_jobs WHERE id=?',(key,))
        if not item: return None
        kinds=(('image','character_reference') if is_image_job(item) else
               ('assemble','episode_assembly') if item['kind'] in ('assemble','episode_assembly') else (item['kind'],))
        return await one(db,'''SELECT * FROM cartoon_assets WHERE job_id=? AND project_id=? AND target=? AND target_id=?
          AND kind IN ('''+','.join('?' for _ in kinds)+') ORDER BY rowid DESC LIMIT 1',
          (key,item['project_id'],item['target'],item['target_id'],*kinds))


async def bind_job_result(key, asset_id, url):
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        item=await one(db,'SELECT * FROM cartoon_jobs WHERE id=?',(key,))
        asset=await one(db,'SELECT * FROM cartoon_assets WHERE id=? AND job_id=?',(asset_id,key))
        if not item or not asset or any(item[k]!=asset[k] for k in ('project_id','target','target_id')):
            raise ValueError('Результат не принадлежит этому заданию.')
        changed=item['result_url']!=url or asset['public_url']!=url
        await db.execute('UPDATE cartoon_assets SET public_url=? WHERE id=?',(url,asset_id))
        await db.execute('UPDATE cartoon_jobs SET result_url=? WHERE id=?',(url,key))
        if changed:
            await db.execute('INSERT INTO cartoon_job_events(job_id,event,details) VALUES (?,?,?)',
                (key,'result_published',encoded({'asset_id':asset_id,'result_url':url})))
        await db.commit()
        return changed


async def approve_character_result(key, actor):
    asset=await job_result(key)
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        item=await one(db,'''SELECT j.*,p.owner_id FROM cartoon_jobs j JOIN cartoon_projects p ON p.id=j.project_id WHERE j.id=?''',(key,))
        if (not item or item['owner_id']!=actor or item['status']!='done' or item['target']!='character'
                or not is_image_job(item) or not asset):
            raise ValueError('Готовый эталон этого задания недоступен.')
        char=await one(db,'SELECT * FROM cartoon_characters WHERE id=? AND project_id=?',(item['target_id'],item['project_id']))
        if not char: raise ValueError('Персонаж не найден.')
        spec=json.loads(char['spec'])
        await db.execute('UPDATE cartoon_assets SET approved=1 WHERE id=?',(asset['id'],))
        if spec.get('reference')!=asset['id']:
            spec['reference']=asset['id']
            revision=char['revision']+1
            await db.execute('UPDATE cartoon_characters SET spec=?,revision=? WHERE id=?',(encoded(spec),revision,char['id']))
            await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',
                             ('character',char['id'],revision,encoded(spec)))
        await db.commit()
        return asset['id']


async def confirm(key, actor):
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        item = await one(db, 'SELECT * FROM cartoon_jobs WHERE id=?', (key,))
        if not item:
            raise ValueError('Задание не найдено.')
        project = await one(db, 'SELECT * FROM cartoon_projects WHERE id=?', (item['project_id'],))
        if actor != project['owner_id']:
            raise ValueError('Нет доступа.')
        if item['status'] != 'quoted':
            return item
        snap = await _snapshot(db, item['target'], item['target_id'])
        snap['quote'] = json.loads(item['snapshot'])['quote']
        if item['expires'] < time.time() or digest(snap) != item['fingerprint']:
            await db.execute("UPDATE cartoon_jobs SET status='stale' WHERE id=?", (key,))
            await db.commit()
            raise ValueError('Оценка устарела. Откройте действие заново.')
        if await liability(db, project['id']) + item['estimate'] > project['budget']:
            raise ValueError('Недостаточно бюджета с учётом резервов и несверенных расходов.')
        await db.execute("UPDATE cartoon_jobs SET status='queued',approved_by=? WHERE id=?", (actor, key))
        await db.execute('INSERT INTO cartoon_job_events(job_id,event,details) VALUES (?,?,?)',
                         (key, 'confirmed', encoded(dict(actor=actor, estimate=item['estimate']))))
        await db.commit()
    return await job(key)


async def cancel_quote(key, actor):
    async with connect() as db:
        result = await db.execute('''UPDATE cartoon_jobs SET status='cancelled' WHERE id=? AND status='quoted'
           AND project_id IN (SELECT id FROM cartoon_projects WHERE owner_id=?)''', (key,actor))
        if not result.rowcount:
            raise ValueError('Оценка уже подтверждена/отменена или недоступна.')
        await db.commit()


async def update_job(key, **fields):
    allowed = {'status','provider_id','result_url','actual','usage','lease_until','attempts','next_attempt','error','notified'}
    if not fields or set(fields) - allowed:
        raise ValueError('Некорректные поля задания.')
    async with connect() as db:
        await db.execute('UPDATE cartoon_jobs SET ' + ','.join(k+'=?' for k in fields) + ' WHERE id=?', (*fields.values(), key))
        await db.execute('INSERT INTO cartoon_job_events(job_id,event,details) VALUES (?,?,?)',
                         (key, fields.get('status', 'updated'), encoded(fields)))
        await db.commit()


async def reconcile(key, actor, actual):
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        item = await one(db, 'SELECT j.*,p.owner_id FROM cartoon_jobs j JOIN cartoon_projects p ON p.id=j.project_id WHERE j.id=?', (key,))
        if not item or item['owner_id'] != actor or item['status'] not in ('done','manual','failed'):
            raise ValueError('Сверять расходы можно только у завершённых/ручных заданий своего проекта.')
        if not isinstance(actual, int) or not 0 <= actual <= 100000000000:
            raise ValueError('Некорректный расход.')
        await db.execute('UPDATE cartoon_jobs SET actual=? WHERE id=?', (actual, key))
        await db.execute('INSERT INTO cartoon_job_events(job_id,event,details) VALUES (?,?,?)',
                         (key, 'cost_reconciled', encoded(dict(actor=actor, previous=item['actual'], actual=actual))))
        await db.commit()


async def video_busy(db, exclude):
    return await one(db, '''SELECT id FROM cartoon_jobs WHERE kind='video' AND id!=? AND
      (status IN ('preparing','submitting','polling') OR (status='manual' AND result_url IS NULL)) LIMIT 1''', (exclude,))


async def claim():
    now = int(time.time())
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        await db.execute("UPDATE cartoon_jobs SET status='manual',error='unknown_submission',lease_until=0 WHERE status='submitting' AND lease_until<?", (now,))
        await db.execute("UPDATE cartoon_jobs SET status='queued',lease_until=0 WHERE status='preparing' AND lease_until<?", (now,))
        candidates = await rows(db, "SELECT * FROM cartoon_jobs WHERE status IN ('queued','polling','publishing') AND lease_until<? AND next_attempt<=? ORDER BY rowid LIMIT 20", (now, now))
        for item in candidates:
            # One studio Kling task IN FLIGHT, not just one concurrent HTTP POST.
            if item['kind']=='video' and item['status']=='queued' and await video_busy(db,item['id']):
                continue
            project = await one(db, 'SELECT * FROM cartoon_projects WHERE id=?', (item['project_id'],))
            if item['status'] == 'queued' and item['estimate'] and await liability(db, project['id']) > project['budget']:
                continue
            status = 'preparing' if item['status'] == 'queued' else item['status']
            await db.execute('UPDATE cartoon_jobs SET status=?,lease_until=? WHERE id=?', (status, now+120, item['id']))
            await db.commit()
            return dict(item, status=status)
        await db.commit()


async def begin_submission(key):
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        item = await one(db, 'SELECT * FROM cartoon_jobs WHERE id=?', (key,))
        if not item or item['status'] != 'preparing' or not item['approved_by']:
            raise ValueError('Нет подтверждения на платный запрос.')
        project = await one(db, 'SELECT * FROM cartoon_projects WHERE id=?', (item['project_id'],))
        if item['kind']=='video' and await video_busy(db,key):
            await db.execute("UPDATE cartoon_jobs SET status='queued',lease_until=0 WHERE id=?", (key,))
            await db.commit()
            return False
        if await liability(db, project['id']) > project['budget']:
            await db.execute("UPDATE cartoon_jobs SET status='queued',lease_until=0 WHERE id=?", (key,))
            await db.commit()
            return False
        await db.execute("UPDATE cartoon_jobs SET status='submitting' WHERE id=?", (key,))
        await db.commit()
        return True


async def add_asset(project, target, key, kind, context_hash, path, sha256, **fields):
    asset_id = uuid4().hex
    async with connect() as db:
        cols = ['id','project_id','target','target_id','kind','context_hash','path','sha256', *fields]
        await db.execute(f'INSERT INTO cartoon_assets ({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})',
                         (asset_id,project,target,key,kind,context_hash,path,sha256,*fields.values()))
        await db.commit()
    return asset_id


async def approve_asset(key, actor, *, expected_kind=None):
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        result = await db.execute('''UPDATE cartoon_assets SET approved=1 WHERE id=? AND project_id IN
          (SELECT id FROM cartoon_projects WHERE owner_id=?)'''
          + (' AND kind=?' if expected_kind is not None else ''),
          (key,actor,expected_kind) if expected_kind is not None else (key,actor))
        if not result.rowcount:
            raise ValueError('Нет доступа к изображению.')
        asset = await one(db,'SELECT * FROM cartoon_assets WHERE id=?',(key,))
        if asset['target']=='scene' and asset['kind']=='image':
            await db.execute('INSERT OR REPLACE INTO cartoon_scene_image_selections VALUES (?,?,?)',
                             (asset['target_id'],asset['context_hash'],key))
        await db.commit()


async def job_list(project):
    async with connect() as db:
        return await rows(db, 'SELECT * FROM cartoon_jobs WHERE project_id=? ORDER BY rowid DESC LIMIT 15', (project,))


async def prepare_drafts(project, settings):
    targets = [('character', c['id']) for c in await children('character', project)]
    for episode in await children('episode', project):
        targets += [('scene', s['id']) for s in await children('scene', episode['id'])]
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        for target,key in targets:
            if await one(db, "SELECT id FROM cartoon_jobs WHERE target=? AND target_id=? AND kind='image'", (target,key)):
                continue
            snap = await _snapshot(db,target,key)
            snap['preparation'] = dict(model=settings.image_model, resolution='1024x1536',
                approval_required=True, missing='Эталон персонажа / утверждённые референсы и оценка цены')
            await db.execute('''INSERT INTO cartoon_jobs(id,project_id,target,target_id,kind,snapshot,fingerprint,status,estimate,expires)
              VALUES (?,?,?,?,?,?,?,'draft',0,0)''',
              (uuid4().hex,project,target,key,'image',encoded(snap),digest(snap)))
        await db.commit()


async def retry_poll(key, actor):
    async with connect() as db:
        result = await db.execute('''UPDATE cartoon_jobs SET status='polling',attempts=0,next_attempt=0,lease_until=0,error=NULL
          WHERE id=? AND kind='video' AND provider_id IS NOT NULL AND status='manual'
          AND project_id IN (SELECT id FROM cartoon_projects WHERE owner_id=?)''', (key,actor))
        if not result.rowcount:
            raise ValueError('Нет известной задачи Kling для безопасного опроса.')
        await db.commit()


async def resolve_video(key, actor):
    async with connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        item=await one(db, '''SELECT j.*,p.owner_id FROM cartoon_jobs j JOIN cartoon_projects p ON p.id=j.project_id
          WHERE j.id=?''', (key,))
        if not item or item['owner_id']!=actor or item['kind']!='video' or item['status']!='manual':
            raise ValueError('Нет ручного задания Kling для сверки.')
        if item['actual'] is None:
            raise ValueError('Сначала сверьте фактическую стоимость задания.')
        await db.execute("UPDATE cartoon_jobs SET status='failed',error='provider_reviewed_by_admin' WHERE id=?",(key,))
        await db.execute('INSERT INTO cartoon_job_events(job_id,event,details) VALUES (?,?,?)',
                         (key,'provider_resolved',encoded({'actor':actor,'provider_no_longer_running':True})))
        await db.commit()

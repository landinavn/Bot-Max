"""Only cartoon_* tables. Immutable revisions and transactional budget reservations."""
import hashlib
import json
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import aiosqlite
import database


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    # Publishing an immutable file must not change scene identity or stale a quote.
    def stable(item):
        if isinstance(item, dict):
            return {k:stable(v) for k,v in item.items() if k != 'public_url'}
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
CREATE INDEX IF NOT EXISTS cartoon_jobs_queue ON cartoon_jobs(status,next_attempt,lease_until);
''')
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
    return await one(db, '''SELECT * FROM cartoon_assets WHERE target=? AND target_id=? AND kind=? AND context_hash=?
                         ORDER BY rowid DESC LIMIT 1''', (target, key, kind, context))


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
            first = await one(db, 'SELECT id FROM cartoon_scenes WHERE episode_id=? AND number=1', (row['episode_id'],))
            if first:
                first_snap = await _snapshot(db, 'scene', first['id'])
                location = first_snap['assets'].get('image')
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
    if snap.get('music'):
        snap['assets']['music'] = snap['music']
    if target == 'character' and row['spec'].get('reference'):
        asset = await one(db, 'SELECT * FROM cartoon_assets WHERE id=?', (row['spec']['reference'],))
        if not asset or asset['project_id'] != project_id or asset['kind'] != 'image':
            raise ValueError('Не найден сохранённый референс персонажа.')
        snap['assets']['image'] = asset
    return snap


async def snapshot(target, key):
    async with connect() as db:
        return await _snapshot(db, target, key)


async def quote(target, key, kind, settings):
    if (target, kind) not in {('character','image'),('scene','image'),('scene','video'),('scene','speech'),('episode','assemble')}:
        raise ValueError('Недопустимый тип задания.')
    snap = await snapshot(target, key)
    if kind == 'image' and target == 'scene':
        if not snap['spec'].get('approved') or not snap.get('cast'):
            raise ValueError('Сначала утвердите сценарий и укажите персонажей.')
        if any(not c['assets'].get('image') or not c['assets']['image']['approved'] for c in snap['cast']):
            raise ValueError('Сначала создайте и утвердите эталоны всех персонажей сцены.')
        if snap['number'] > 1 and not snap.get('location_reference'):
            raise ValueError('Сначала создайте и утвердите сцену 1 как эталон квартиры.')
    if kind in ('video','speech') and (not snap['assets'].get('image') or not snap['assets']['image']['approved']):
        raise ValueError('Сначала утвердите изображение этой версии сцены.')
    if kind == 'speech' and not snap['spec'].get('dialogue'):
        raise ValueError('В сцене нет реплик.')
    if kind == 'assemble':
        if not snap.get('scenes') or any(not (s['assets'].get('image') or s['assets'].get('video'))
             or (s['spec'].get('dialogue') and not s['assets'].get('speech')) for s in snap['scenes']):
            raise ValueError('Для каждой сцены нужны изображение/видео и озвучка всех реплик.')
    cost = settings.quote(kind, snap)
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
        candidates = await rows(db, "SELECT * FROM cartoon_jobs WHERE status IN ('queued','polling') AND lease_until<? AND next_attempt<=? ORDER BY rowid LIMIT 20", (now, now))
        for item in candidates:
            # One studio Kling task IN FLIGHT, not just one concurrent HTTP POST.
            if item['kind']=='video' and item['status']=='queued' and await video_busy(db,item['id']):
                continue
            project = await one(db, 'SELECT * FROM cartoon_projects WHERE id=?', (item['project_id'],))
            if item['status'] == 'queued' and item['estimate'] and await liability(db, project['id']) > project['budget']:
                continue
            status = 'preparing' if item['status'] == 'queued' else 'polling'
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


async def approve_asset(key, actor):
    async with connect() as db:
        result = await db.execute('''UPDATE cartoon_assets SET approved=1 WHERE id=? AND project_id IN
          (SELECT id FROM cartoon_projects WHERE owner_id=?)''', (key, actor))
        if not result.rowcount:
            raise ValueError('Нет доступа к изображению.')
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

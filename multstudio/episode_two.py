"""Approved episode two: local preparation only; never submits provider requests."""
from uuid import uuid4

from . import repository as repo
from .tts import normalize_speed

TITLE = 'Мам, а ты чего хочешь?'
SPEEDS = {'Аня': 1.05, 'Дима': 0.95, 'Маша': 1.10}
SCENES = [
    ('Чего же я хочу?', 6, ['Аня'],
     'Та же кухня-гостиная, деревянный стол, заснеженный городской пейзаж за окном, мягкий тёплый свет. Аня сидит за столом и смотрит на блокнот с заголовком «Мои мечты…». Задумчивость и небольшая растерянность.',
     [('Аня', 'Так… чего же я хочу? Даже не знаю, с чего начать', 'Задумчиво, немного растерянно.')],
     'Один средний план Ани за столом.', 'Тёплая спокойная музыка.'),
    ('Желания для всей семьи', 6, ['Аня'],
     'Та же кухня-гостиная. Аня пишет желания для всей семьи. Она оживляется, затем перечитывает список; воодушевление переходит в сомнение.',
     [('Аня', 'Чтобы дети не болели, дома был порядок и все были счастливы…', 'Воодушевлённо, затем с сомнением.')],
     'Один средний план Ани с ручкой и блокнотом.', 'Мягкая семейная музыка.'),
    ('А твои-то где?', 6, ['Аня', 'Маша'],
     'Та же кухня-гостиная. Маша подходит к маме и заглядывает в блокнот. Аня удивлённо смотрит на дочь. Только Аня и Маша, без других людей.',
     [('Маша', 'Мам, это же желания для всех! А твои-то где?', 'Искренне, любознательно.')],
     'Один парный средний план Маши и Ани за столом.', 'Лёгкий музыкальный акцент.'),
    ('Мечта Димы', 6, ['Дима', 'Аня'],
     'Та же кухня-гостиная, часть комнаты с прежним диваном. Дима сидит на диване с телефоном, Аня рядом. Семейный юмор, лёгкая ирония, без конфликта.',
     [('Дима', 'Я вот мечтаю о выходных!', 'Невозмутимо, с юмором.'),
      ('Аня', 'Ты и так каждый вечер отдыхаешь!', 'С доброй иронией, без конфликта.')],
     'Один парный план у дивана, одна камера.', 'Лёгкие комические звуки, семейный юмор.'),
    ('Просыпаться у моря', 7, ['Аня'],
     'Мечта Ани: красивый пляж, море и пальмы. Аня с закрытыми глазами и мечтательной улыбкой, в той же одежде и с прежней внешностью. Это воображение, а не новая квартира. Один цельный кадр пляжа, без коллажа или вставки комнаты.',
     [('Аня', 'А я бы хотела просыпаться у моря… И никуда не спешить!', 'Мечтательно, спокойно, с улыбкой.')],
     'Один спокойный средний план Ани у моря.', 'Мягкая мечтательная музыка.'),
    ('Сделаем мечту видимой', 7, ['Маша', 'Аня'],
     'Возвращение в прежнюю кухню-гостиную, к тому же деревянному столу. Маша достаёт телефон и показывает его маме. Экран телефона зрителю не виден; карта желаний и интерфейс не показаны.',
     [('Маша', 'Мам, а давай сделаем твою мечту видимой?', 'Воодушевлённо и доброжелательно.'),
      ('Аня', 'Это ещё как?', 'С любопытством.')],
     'Один парный план Ани и Маши, телефон повёрнут экраном от зрителя.', 'Мягкая мечтательная музыка.'),
    ('Сейчас покажу!', 6, ['Маша', 'Аня'],
     'Та же кухня-гостиная. Маша нажимает на экран телефона. Мягкое свечение освещает лицо Ани. Экран скрыт от зрителя. Не показывать карту желаний, интерфейс, рекламную заставку или QR-код. Сохранить интригу для третьей серии.',
     [('Маша', 'Смотри, сейчас покажу!', 'С тёплым воодушевлением.')],
     'Один парный план, мягкое свечение на лице Ани.', 'Мягкая мечтательная музыка, затем финальный акцент.'),
]


async def prepare(project_id, actor):
    """Atomic/idempotent insert; no edits to project, cast, episode one or prior media."""
    async with repo.connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        project = await repo.one(db, 'SELECT * FROM cartoon_projects WHERE id=?', (project_id,))
        if not project or project['owner_id'] != actor or project['seed_key'] != 'anya-s1-v1':
            raise ValueError('Нужен существующий проект «Маленькие чудеса Ани» и его администратор.')
        first = await repo.one(db, 'SELECT id FROM cartoon_episodes WHERE project_id=? AND number=1', (project_id,))
        last = await repo.one(db, 'SELECT id FROM cartoon_scenes WHERE episode_id=? ORDER BY number DESC LIMIT 1', (first['id'],)) if first else None
        if not last:
            raise ValueError('Сначала нужна последняя сцена первой серии.')
        cast = {c['name']: c['id'] for c in await repo.rows(db, 'SELECT * FROM cartoon_characters WHERE project_id=?', (project_id,))}
        if not SPEEDS.keys() <= cast.keys():
            raise ValueError('Не найдены сохранённые Аня, Дима и Маша. Новые персонажи не создаются.')
        episode = await repo.one(db, 'SELECT * FROM cartoon_episodes WHERE project_id=? AND number=2', (project_id,))
        if episode and episode['title'] != TITLE:
            raise ValueError('Номер серии 2 уже занят. Существующая серия не изменена.')
        if not episode:
            key = uuid4().hex
            spec = dict(approved=True, target_seconds=44, max_target_seconds=None,
                        ending=['Продолжение следует…'], source='Утверждённый сценарий владельца, серия 2.',
                        production=dict(format='1080x1920 / 30 FPS / H.264 + AAC',
                            audio_first=True, separate_utterances=True, measure_audio=True,
                            no_extra_voice_speedup=True, extend_scenes_for_speech=True,
                            lip_sync='По возможностям текущего Kling; требуется просмотр администратора.',
                            music='Приглушать во время речи; комические акценты в сцене 4; мечтательная музыка 5–7.',
                            quality_review_required=True, advertising=False, qr_code=False))
            await _insert(db, 'episode', key, spec, project_id=project_id, title=TITLE, number=2)
            episode = {'id': key}
        for number, (title, seconds, names, visual, dialogue, camera, sound) in enumerate(SCENES, 1):
            if await repo.one(db, 'SELECT id FROM cartoon_scenes WHERE episode_id=? AND number=?', (episode['id'], number)):
                continue
            spec = dict(title=title, duration=seconds, approved=True, characters=[cast[n] for n in names],
                        visual=visual, camera=camera, sound=sound, trim_video_to_script=True,
                        speech_speeds={cast[n]: SPEEDS[n] for n in names},
                        dialogue=[dict(character_id=cast[n], text=t, direction=d) for n,t,d in dialogue])
            # A fixed approved anchor restores the apartment after the beach dream as well.
            if number != 5:
                spec['location_reference_scene_id'] = last['id']
            else:
                spec['new_location'] = True
            await _insert(db, 'scene', uuid4().hex, spec, episode_id=episode['id'], number=number)
        await db.commit()
    return episode['id']


async def _insert(db, entity, key, spec, **fields):
    cols = ['id', 'spec', *fields]
    await db.execute(f'INSERT INTO {repo.TABLES[entity]} ({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})',
                     (key, repo.encoded(spec), *fields.values()))
    await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',
                     (entity, key, 1, repo.encoded(dict(spec=spec, **fields))))


async def speech_profiles(snap):
    profiles = {}
    for char in snap.get('cast', []):
        profile = await repo.character_speech(char['id'])
        if char['id'] in snap['spec'].get('speech_speeds', {}):
            profile['speed'] = normalize_speed(snap['spec']['speech_speeds'][char['id']])
        profiles[char['id']] = dict(profile, name=(await repo.get('character', char['id']))['name'])
    return profiles


async def plan(episode_id, settings):
    episode = await repo.snapshot('episode', episode_id)
    result = dict(images=0, videos=0, utterances=0, services={}, errors=[], total=0)
    for scene in episode['scenes']:
        result['images'] += 1; result['videos'] += 1
        result['utterances'] += len(scene['spec'].get('dialogue', []))
        for kind in ('image', 'video', 'speech'):
            if kind == 'speech' and not scene['spec'].get('dialogue'): continue
            try:
                q = settings.quote(kind, scene, speech_profiles=await speech_profiles(scene)) if kind == 'speech' else settings.quote(kind, scene)
                bucket = result['services'].setdefault(kind, dict(estimate=0, models=set(), resolutions=set(), duration=0))
                bucket['duration'] += q['duration']; bucket['estimate'] += q['estimate']; bucket['models'].add(q['model']); bucket['resolutions'].add(q['resolution'])
                result['total'] += q['estimate']
            except ValueError as exc:
                if str(exc) not in result['errors']: result['errors'].append(str(exc))
    return result


async def prepare_jobs(episode_id, settings):
    """Drafts cannot be confirmed/claimed; estimates are refreshed on explicit quote."""
    episode = await repo.snapshot('episode', episode_id)
    for snap in [*episode['scenes'], episode]:
        kinds = ('image', 'video', 'speech') if snap['target'] == 'scene' else ('assemble',)
        for kind in kinds:
            profiles = await speech_profiles(snap) if kind == 'speech' else None
            try:
                q = settings.quote(kind, snap, speech_profiles=profiles)
                error = None
            except ValueError as exc:
                q = dict(estimate=None); error = str(exc)
            frozen = dict(snap, preparation=dict(kind=kind, approval_required=True, quote=q,
                speech_profiles=profiles, error=error, audio_before_animation=True))
            async with repo.connect() as db:
                await db.execute('BEGIN IMMEDIATE')
                if not await repo.one(db, 'SELECT id FROM cartoon_jobs WHERE target=? AND target_id=? AND kind=?', (snap['target'], snap['id'], kind)):
                    await db.execute('''INSERT INTO cartoon_jobs(id,project_id,target,target_id,kind,snapshot,fingerprint,status,estimate,expires)
                        VALUES (?,?,?,?,?,?,?,'draft',?,0)''',
                        (uuid4().hex, snap['project_id'], snap['target'], snap['id'], kind,
                         repo.encoded(frozen), repo.digest(frozen), q['estimate'] or 0))
                await db.commit()

"""Replace only the unpublished fourth draft; archive previous revisions and files."""
from uuid import uuid4
import json
from . import repository as repo
from .episode_two import SPEEDS, _insert
from .rules import normalize
from .timeline import ordered

TITLE = 'Мечты больше не ждут!'
MIGRATION = 'anya-s1-e4-dreams-now-v1'
OPENAI_BLOCK = ('Это устаревшее задание OpenAI для сцены Маши. Запрос не отправлен. '
                'Текущий маршрут — Kling Avatar с утверждённым MP3. '
                'Обновите черновик и создайте новое подтверждение видео; старое задание не перенаправляется автоматически.')
SCENES = [
    ('Хватит откладывать!', 'Аня', 'Всё! Хватит откладывать. Сделаю карту желаний!', 'medium_wide',
     'Аня за прежним столом с блокнотом и телефоном. Снег за окном, тёплые новогодние огни. Исходный момент: блокнот закрыт, телефон в руке.',
     'Средне-общий план, лицо в три четверти хорошо видно, рот не закрыт предметами.',
     'Аня решительно закрывает блокнот, берёт телефон, энергично улыбается. Небольшое приближение от общего к среднему плану.'),
    ('Маша помогает', 'Маша', 'Мам, добавь море, своё дело и время для себя!', 'medium',
     'Маша одна на прежнем диване в бирюзовом свитере, высоком хвосте и тёмных брюках. Смотрит к маме за кадром с закрытой улыбкой.',
     'Средний план Маши; хорошо видно лицо, руки и знакомую гостиную. Других людей нет.',
     'Маша с радостью, интересом и желанием помочь подсказывает маме за кадром. Только её реплика.'),
    ('Создаём!', 'Аня', 'Фото загрузила. Желания написала. Создаём!', 'medium_side',
     'Аня за прежним столом проверяет желания на телефоне. Экран отвёрнут от зрителя; интерфейс не выдумывать.',
     'Средний боковой план, лицо и рот видны в три четверти, телефон ниже лица.',
     'Аня проверяет желания и один раз нажимает на экран. Радостное ожидание.'),
    ('Карта создаётся', None, '', 'montage',
     'Настоящие утверждённые изображения из бота: Аня у моря, в своей творческой студии и на отдыхе с семьёй. Полноценная композиция трёх желаний.',
     'Предметный монтаж трёх готовых изображений, плавная сборка карты, без людей, говорящих в кадре.',
     'Три изображения последовательно появляются и собираются в коллаж. Мягкое движение камеры. Только общая музыка.'),
    ('Это действительно я!', 'Аня', 'Это же я! И на море, и в своей студии!', 'medium',
     'Аня за прежним столом с телефоном смотрит на готовую карту. Лицо узнаваемое, счастливая закрытая улыбка в исходной картинке.',
     'Средний план Ани, видны руки, телефон и часть комнаты; не крупный портрет.',
     'Искреннее счастливое удивление. После реплики перейти к немому плану настоящей карты.'),
    ('Дима тоже заинтересовался', 'Дима', 'А меня на карте рядом с морем разместишь?', 'medium_wide',
     'Дима один рядом с прежним диваном, в тёмно-синем свитшоте и серых домашних брюках. Смотрит к Ане за кадром.',
     'Средне-общий план, лицо и рот хорошо различимы, видна гостиная.',
     'Спокойная добродушная шутка, лёгкий жест и улыбка. После окончания речи — немой план карты.'),
    ('Моя карта желаний', None, '', 'object_topdown',
     'На прежнем деревянном столе телефон, блокнот, распечатанные изображения настоящих желаний Ани. Без людей, без выдуманного интерфейса и генерируемых надписей.',
     'Предметный план стола с плавным приближением, затем крупная и чёткая готовая карта. Без быстрого мелькания.',
     'Плавный переход от стола к готовой карте. Изображения мечтаний остаются открытыми; музыка мягко завершается.'),
]


async def apply(project_id, actor):
    async with repo.connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        project = await repo.one(db,'SELECT * FROM cartoon_projects WHERE id=?',(project_id,))
        if not project or project['owner_id'] != actor or project['seed_key'] != 'anya-s1-v1':
            raise ValueError('Нужен существующий проект Ани и его администратор.')
        episode = repo.unpack(await repo.one(db,'SELECT * FROM cartoon_episodes WHERE project_id=? AND number=4',(project_id,)))
        if not episode: raise ValueError('Сначала подготовьте исходный черновик серии 4.')
        if await repo.one(db,'SELECT name FROM cartoon_editing_migrations WHERE name=?',(MIGRATION,)):
            return await _upgrade_masha(db,episode)
        from .episode_four import TITLE as OLD_TITLE
        if episode['title'] != OLD_TITLE: raise ValueError('Номер 4 занят другим сценарием; серия сохранена.')
        published = await repo.one(db,"SELECT id FROM cartoon_assets WHERE target='episode' AND target_id=? AND kind='assemble' LIMIT 1",(episode['id'],))
        running = await repo.one(db,"""SELECT id FROM cartoon_jobs WHERE
            (target_id=? OR target_id IN (SELECT id FROM cartoon_scenes WHERE episode_id=?)) AND
            (status IN ('queued','preparing','submitting','polling','avatar_polling','video_preparing','publishing')
             OR (status='manual' AND provider_id IS NOT NULL)) LIMIT 1""",(episode['id'],episode['id']))
        if published or running:
            raise ValueError('Серия 4 уже собрана или содержит незавершённые запросы провайдеров. Автозамена черновика остановлена; результаты и задания сохранены.')
        cast = {c['name']:c['id'] for c in await repo.rows(db,'SELECT id,name FROM cartoon_characters WHERE project_id=?',(project_id,))}
        if not {'Аня','Дима','Маша'} <= cast.keys(): raise ValueError('Не найдены сохранённые Аня, Дима и Маша.')
        previous = repo.unpack(await repo.one(db,'SELECT * FROM cartoon_episodes WHERE project_id=? AND number=3',(project_id,)))
        if not previous: raise ValueError('Нужна третья серия для непрерывности квартиры и изображения моря.')
        earlier = ordered(await repo.rows(db,'SELECT id,number FROM cartoon_scenes WHERE episode_id=?',(previous['id'],)),previous['spec'])
        beach = await repo.one(db,'SELECT id FROM cartoon_scenes WHERE episode_id=? AND number=5',(previous['id'],))
        if not earlier or not beach: raise ValueError('Не найдены последний кадр и сцена моря третьей серии.')
        old = [repo.unpack(s) for s in await repo.rows(db,'SELECT * FROM cartoon_scenes WHERE episode_id=?',(episode['id'],))]
        by_number = {s['number']:s for s in old}
        if any(n not in by_number for n in range(1,8)): raise ValueError('Исходный черновик неполон, автоматическая замена отменена.')
        archived_editing={}
        for number in range(1,8):
            record=await repo.one(db,'SELECT speech_start,video_start FROM cartoon_scene_editing WHERE scene_id=?',(by_number[number]['id'],))
            if record:archived_editing[by_number[number]['id']]=record
        prior_studio=(await repo._snapshot(db,'scene',by_number[7]['id']))['assets'].get('image')
        prior_beach=(await repo._snapshot(db,'scene',beach['id']))['assets'].get('image')
        reusable=[prior_beach,prior_studio,None]
        auxiliary=[]
        for index,(title,visual) in enumerate([
            ('Материал карты: море','Фотография желаемого будущего: узнаваемая Аня у моря на солнечном пляже. Её утверждённое лицо, голубые глаза, причёска и одежда, без надписей.'),
            ('Материал карты: творческая студия','Фотография желаемого будущего: узнаваемая Аня в собственной светлой творческой студии, у рабочего стола, ноутбука и растений. Та же внешность, одежда и лицо. Без надписей.'),
            ('Материал карты: семейный отдых','Фотография желаемого будущего: узнаваемые Аня, Дима и Маша отдыхают вместе в уютном загородном доме. Те же лица и одежда, естественные закрытые улыбки. Без текста и лишних людей.')],101):
            key=uuid4().hex;auxiliary.append(key)
            names=['Аня'] if index in (101,102) else ['Аня','Дима','Маша']
            spec=normalize(dict(title=title,approved=True,visual=visual,duration=5,new_location=True,
                characters=[cast[n] for n in names],dialogue=[],material_only=True,framing='medium_wide'))
            await _insert(db,'scene',key,spec,episode_id=episode['id'],number=index)
            source=reusable[index-101]
            if source and source.get('approved'):
                snap=await repo._snapshot(db,'scene',key)
                await db.execute('''INSERT INTO cartoon_assets
                    (id,project_id,target,target_id,kind,context_hash,path,sha256,public_url,approved,metadata)
                    VALUES (?,?,'scene',?,'image',?,?,?,?,1,?)''',
                    (uuid4().hex,project_id,key,snap['context_hash'],source['path'],source['sha256'],source.get('public_url'),repo.encoded({'reused_approved_asset_id':source['id']})))
        for number,(title,speaker,text,framing,visual,camera,motion) in enumerate(SCENES,1):
            row=by_number[number]
            spec=dict(title=title,approved=True,duration=5,visual=visual,framing=framing,camera_direction=camera,
                motion=motion,characters=[cast[speaker]] if speaker else [],
                dialogue=[dict(character_id=cast[speaker],text=text,direction='Естественно, эмоционально, без изменения утверждённой скорости.')] if speaker else [],
                speech_speeds={cast[speaker]:SPEEDS[speaker]} if speaker else {},
                location_reference_scene_id=earlier[-1]['id'],episode_four_revision=MIGRATION,
                sound='Только общая музыка.' if not speaker else 'Тёплая музыка с приглушением во время речи.')
            if speaker=='Маша': spec.update(kling_audio_required=True)
            if number in (4,7): spec.update(local_dream_video=True,dream_materials_required=True)
            if number==4: spec.update(local_dream_image=True,new_location=True);spec.pop('location_reference_scene_id')
            if speaker: spec.update(tail_cutaway_scene_id=by_number[4]['id'])
            if number==7: spec['final_dream_card']=True
            await db.execute('''INSERT INTO cartoon_scene_editing(scene_id,speech_start,video_start) VALUES (?,0,0)
                ON CONFLICT(scene_id) DO UPDATE SET speech_start=0,video_start=0''',(row['id'],))
            spec=normalize(spec);revision=row['revision']+1
            await db.execute('UPDATE cartoon_scenes SET spec=?,revision=? WHERE id=?',(repo.encoded(spec),revision,row['id']))
            await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',('scene',row['id'],revision,repo.encoded(spec)))
        active=[by_number[n]['id'] for n in range(1,8)]
        excluded=[s['id'] for s in old if s['id'] not in active]+auxiliary
        spec=dict(episode['spec'],target_seconds=35,scene_order=active,excluded_scene_ids=excluded,
            dream_scene_ids=auxiliary,ending=['А вы уже создали свою карту желаний?','Продолжение следует…','Открытки и Карта желаний | Нейрофото'],
            production=dict(audio_first=True,strict_five_seconds=True,quality_review_required=True,episode_four_revision=MIGRATION),
            revision_note=TITLE,revision_archive_title=episode['title'],archived_scene_editing=archived_editing)
        spec.pop('closing_card',None);spec.pop('heygen_end_trim_seconds',None);spec.pop('max_target_seconds',None)
        revision=episode['revision']+1
        await db.execute('UPDATE cartoon_episodes SET title=?,spec=?,revision=? WHERE id=?',(TITLE,repo.encoded(spec),revision,episode['id']))
        await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',('episode',episode['id'],revision,repo.encoded(spec)))
        await db.execute("""UPDATE cartoon_jobs SET status='cancelled',error='episode_four_draft_replaced',lease_until=0
            WHERE (target_id=? OR target_id IN (SELECT id FROM cartoon_scenes WHERE episode_id=?)) AND status IN ('draft','quoted')""",(episode['id'],episode['id']))
        await db.execute('INSERT INTO cartoon_editing_migrations VALUES (?,?)',(MIGRATION,project_id))
        await db.commit()
    return episode['id']


async def _upgrade_masha(db, episode):
    """Upgrade the already installed OpenAI draft without regenerating image or voice."""
    row=repo.unpack(await repo.one(db,'SELECT * FROM cartoon_scenes WHERE episode_id=? AND number=2',(episode['id'],)))
    if not row or row['spec'].get('video_provider')!='openai':return episode['id']
    if row['spec'].get('episode_four_revision')!=MIGRATION:return episode['id']
    busy=await repo.one(db,"""SELECT id FROM cartoon_jobs WHERE target_id=? AND
        (status IN ('queued','preparing','submitting','polling','publishing') OR (status='manual' AND provider_id IS NOT NULL)) LIMIT 1""",(row['id'],))
    before=await repo._snapshot(db,'scene',row['id'])
    if busy or before['assets'].get('video'):
        raise ValueError('У сцены Маши уже есть видео или незавершённый запрос. Материалы сохранены; дождитесь завершения перед сменой маршрута.')
    cutaway=await repo.one(db,'SELECT id FROM cartoon_scenes WHERE episode_id=? AND number=4',(episode['id'],))
    if not cutaway:raise ValueError('Не найдена карта для немого плана после речи Маши.')
    spec=dict(row['spec'],kling_audio_required=True,tail_cutaway_scene_id=cutaway['id'])
    spec.pop('openai_audio_sync_required',None);spec.pop('provider_block_reason',None)
    spec=normalize(spec);revision=row['revision']+1
    await db.execute('UPDATE cartoon_scenes SET spec=?,revision=? WHERE id=?',(repo.encoded(spec),revision,row['id']))
    await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',('scene',row['id'],revision,repo.encoded(spec)))
    current=await repo._snapshot(db,'scene',row['id'])
    for kind in ('image','speech'):
        source=before['assets'].get(kind)
        if not source:continue
        metadata=json.loads(source.get('metadata') or '{}')
        metadata['route_change_source_asset_id']=source['id']
        await db.execute('''INSERT INTO cartoon_assets
            (id,project_id,target,target_id,kind,context_hash,path,sha256,public_url,approved,metadata)
            VALUES (?,?,'scene',?,?,?,?,?,?,?,?)''',
            (uuid4().hex,before['project_id'],row['id'],kind,current['context_hash'],source['path'],source['sha256'],source.get('public_url'),source['approved'],repo.encoded(metadata)))
    await db.execute("UPDATE cartoon_jobs SET status='cancelled',error='masha_route_changed_to_kling' WHERE target_id=? AND status IN ('quoted','draft')",(row['id'],))
    await db.commit()
    return episode['id']

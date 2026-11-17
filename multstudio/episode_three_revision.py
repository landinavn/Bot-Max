"""One owner-approved revision: Anya speaks shot 3; two silent 5 s inserts.

Stable scene numbers/IDs and untouched media hashes are preserved. Playback
order belongs to the episode, so inserting a shot cannot orphan later media.
"""
from uuid import uuid4

from . import repository as repo
from .episode_three import TITLE, SCENES
from .episode_two import _insert
from .rules import normalize
from .timeline import ordered

MIGRATION='anya-s1-e3-anya-medium-broll-20261117-v1'
PAIR_TITLE='Вместе смотрим в телефон'
ROOM_TITLE='Уютная квартира — движение'


async def apply(project_id, actor):
    async with repo.connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        project=await repo.one(db,'SELECT * FROM cartoon_projects WHERE id=?',(project_id,))
        if not project or project['owner_id']!=actor or project['seed_key']!='anya-s1-v1':
            raise ValueError('Нет доступа к исходному проекту Ани.')
        episode=repo.unpack(await repo.one(db,'SELECT * FROM cartoon_episodes WHERE project_id=? AND number=3',(project_id,)))
        if not episode or episode['title']!=TITLE: raise ValueError('Не найдена исходная третья серия.')
        if await repo.one(db,'SELECT name FROM cartoon_editing_migrations WHERE name=?',(MIGRATION,)):
            return episode['id']
        scenes=[repo.unpack(s) for s in await repo.rows(db,'SELECT * FROM cartoon_scenes WHERE episode_id=? ORDER BY number',(episode['id'],))]
        if len(scenes)!=9 or {s['number'] for s in scenes}!=set(range(1,10)):
            raise ValueError('Состав третьей серии уже изменён. Нужны исходные 9 сцен; правки не перезаписаны.')
        cast={c['name']:c['id'] for c in await repo.rows(db,'SELECT * FROM cartoon_characters WHERE project_id=?',(project_id,))}
        if not {'Аня','Маша'}<=cast.keys(): raise ValueError('Не найдены сохранённые Аня и Маша.')
        third=next(s for s in scenes if s['number']==3)
        lines=third['spec'].get('dialogue',[])
        if len(lines)!=1 or not lines[0].get('text'):
            raise ValueError('В сцене 3 изменилась структура реплики. Существующие данные сохранены.')
        spec=dict(third['spec'],characters=[cast['Аня']],speaker_id=cast['Аня'],
                  speech_speeds={cast['Аня']:1.05},framing='medium',visual=SCENES[2][3],
                  dialogue=[dict(lines[0],character_id=cast['Аня'])])
        spec=normalize(spec)
        if spec!=third['spec']:
            revision=third['revision']+1
            await db.execute('UPDATE cartoon_scenes SET spec=?,revision=? WHERE id=?',(repo.encoded(spec),revision,third['id']))
            await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',('scene',third['id'],revision,repo.encoded(spec)))
        # Cancel only pre-submission work for the replaced shot; preserve paid tasks and every result.
        pending=await repo.rows(db,"SELECT id FROM cartoon_jobs WHERE target='scene' AND target_id=? AND status IN ('quoted','queued','preparing')",(third['id'],))
        for job in pending:
            await db.execute("UPDATE cartoon_jobs SET status='cancelled',lease_until=0,error='scene_revised_before_submission' WHERE id=?",(job['id'],))
            await db.execute('INSERT INTO cartoon_job_events(job_id,event,details) VALUES (?,?,?)',
                             (job['id'],'scene_revision_cancelled',repo.encoded({'actor':actor,'scene_id':third['id']})))
        anchor=third['spec'].get('location_reference_scene_id')
        if not anchor: raise ValueError('Не найден прежний референс квартиры из второй серии.')
        pair_id,room_id=uuid4().hex,uuid4().hex
        pair=normalize(dict(title=PAIR_TITLE,approved=True,duration=5,display_number='2а',
            framing='medium',characters=[cast['Аня'],cast['Маша']],dialogue=[],
            location_reference_scene_id=anchor,
            visual='Один средний парный план Ани и Маши по пояс за прежним столом. '
                   'Аня в кремовом свитере держит телефон; Маша в бирюзовом свитере рядом с интересом смотрит на него. '
                   'Видны руки, телефон, край стола и прежняя кухня-гостиная. Экран повёрнут от зрителя. '
                   'Обе слегка улыбаются с закрытыми губами, зубы не видны. Никто не говорит.',
            camera='Один средний парный план, без коллажа и крупного плана лица.',
            motion='Аня слегка поворачивает свой телефон к Маше; Маша переводит взгляд на экран и мягко кивает. '
                   'Телефон один, положение рук почти неизменно. Небольшое естественное движение взглядов и камеры. '
                   'Губы остаются сомкнутыми. Без речи и lip-sync.',
            sound='Без голоса и звука генератора; только общая музыка при финальном монтаже.'))
        room=normalize(dict(title=ROOM_TITLE,approved=True,duration=5,display_number='6а',
            framing='wide',characters=[],dialogue=[],location_reference_scene_id=anchor,
            visual='Один общий план прежней кухни-гостиной без людей: деревянный стол, телефон, чашка кофе, '
                   'диван, тёплые лампы и заснеженное окно. Та же планировка и мебель. '
                   'Без надписей, интерфейса, новых предметов или коллажа.',
            camera='Общий план всей комнаты, видны окно, стол и диван.',
            motion='Медленный плавный проезд камеры вдоль стола к заснеженному окну. '
                   'Тонкий пар над чашкой, мягкое мерцание света и снег за окном. Люди не появляются. Без речи.',
            sound='Без голоса и звука генератора; только общая фоновая музыка при монтаже.'))
        await _insert(db,'scene',pair_id,pair,episode_id=episode['id'],number=10)
        await _insert(db,'scene',room_id,room,episode_id=episode['id'],number=11)
        timeline=[]
        for scene in ordered(scenes,episode['spec']):
            timeline.append(scene['id'])
            if scene['number']==2: timeline.append(pair_id)
            if scene['number']==6: timeline.append(room_id)
        episode_spec=dict(episode['spec'],scene_order=timeline,target_seconds=55,
                          revision_note='Реплика 3 передана Ане; средний план; две немые пятисекундные вставки.')
        revision=episode['revision']+1
        await db.execute('UPDATE cartoon_episodes SET spec=?,revision=? WHERE id=?',(repo.encoded(episode_spec),revision,episode['id']))
        await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',('episode',episode['id'],revision,repo.encoded(episode_spec)))
        await db.execute('INSERT INTO cartoon_editing_migrations VALUES (?,?)',(MIGRATION,project_id))
        await db.commit()
    return episode['id']

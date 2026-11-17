"""Remove insert 2а from playback without deleting its scene, jobs or media."""
from . import repository as repo
from .episode_three import TITLE
from .episode_three_revision import PAIR_TITLE
from .timeline import ordered

MIGRATION='anya-s1-e3-remove-insert-2a-20261117-v1'


async def apply(project_id,actor):
    async with repo.connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        project=await repo.one(db,'SELECT * FROM cartoon_projects WHERE id=?',(project_id,))
        if not project or project['owner_id']!=actor or project['seed_key']!='anya-s1-v1':
            raise ValueError('Нет доступа к исходному проекту Ани.')
        episode=repo.unpack(await repo.one(db,'SELECT * FROM cartoon_episodes WHERE project_id=? AND number=3',(project_id,)))
        if not episode or episode['title']!=TITLE:raise ValueError('Не найдена третья серия.')
        if await repo.one(db,'SELECT name FROM cartoon_editing_migrations WHERE name=?',(MIGRATION,)):
            return episode['id']
        scenes=[repo.unpack(s) for s in await repo.rows(db,'SELECT * FROM cartoon_scenes WHERE episode_id=?',(episode['id'],))]
        pairs=[s for s in scenes if s['number']==10 and s['spec'].get('display_number')=='2а' and s['spec'].get('title')==PAIR_TITLE]
        if len(pairs)!=1:raise ValueError('Не найдена исходная вставка 2а; существующие сцены сохранены.')
        pair=pairs[0]
        remaining=[s for s in ordered(scenes,episode['spec']) if s['id']!=pair['id']]
        excluded=list(episode['spec'].get('excluded_scene_ids',[]))
        if pair['id'] not in excluded:excluded.append(pair['id'])
        spec=dict(episode['spec'],scene_order=[s['id'] for s in remaining],excluded_scene_ids=excluded,
                  target_seconds=sum(s['spec']['duration'] for s in remaining),
                  revision_note='Вставка 2а исключена из монтажа; файлы и задания сохранены в архиве. Вставка 6а остаётся.')
        revision=episode['revision']+1
        await db.execute('UPDATE cartoon_episodes SET spec=?,revision=? WHERE id=?',(repo.encoded(spec),revision,episode['id']))
        await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',('episode',episode['id'],revision,repo.encoded(spec)))
        # Do not start removed footage or an obsolete assembly; never cancel a submitted paid request.
        jobs=await repo.rows(db,"""SELECT id FROM cartoon_jobs WHERE project_id=?
            AND status IN ('quoted','queued','preparing') AND
            ((target='scene' AND target_id=?) OR (target='episode' AND target_id=? AND kind='assemble'))""",
            (project_id,pair['id'],episode['id']))
        for job in jobs:
            await db.execute("UPDATE cartoon_jobs SET status='cancelled',lease_until=0,error='scene_removed_from_timeline' WHERE id=?",(job['id'],))
            await db.execute('INSERT INTO cartoon_job_events(job_id,event,details) VALUES (?,?,?)',
                             (job['id'],'scene_removed_from_timeline',repo.encoded({'actor':actor,'scene_id':pair['id']})))
        await db.execute('INSERT INTO cartoon_editing_migrations VALUES (?,?)',(MIGRATION,project_id))
        await db.commit()
    return episode['id']

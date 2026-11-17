"""One owner-requested montage revision; never regenerate media or edit scene specs."""
from . import repository as repo
from .episode_three import TITLE
from config import validate_bot_link

MIGRATION='anya-s1-e3-short-heygen-tail-russian-qr-20261117-v1'


async def apply(project_id,actor,bot_url):
    bot_url=validate_bot_link(bot_url)
    async with repo.connect() as db:
        await db.execute('BEGIN IMMEDIATE')
        project=await repo.one(db,'SELECT * FROM cartoon_projects WHERE id=?',(project_id,))
        if not project or project['owner_id']!=actor or project['seed_key']!='anya-s1-v1':
            raise ValueError('Нет доступа к исходному проекту Ани.')
        episode=repo.unpack(await repo.one(db,'SELECT * FROM cartoon_episodes WHERE project_id=? AND number=3',(project_id,)))
        if not episode or episode['title']!=TITLE:raise ValueError('Не найдена третья серия.')
        if await repo.one(db,'SELECT name FROM cartoon_editing_migrations WHERE name=?',(MIGRATION,)):
            return episode['id']
        spec=dict(episode['spec'],heygen_end_trim_seconds=2,
                  closing_card={'style':'anya-russian-qr-v1','bot_url':bot_url,'seconds':5})
        revision=episode['revision']+1
        await db.execute('UPDATE cartoon_episodes SET spec=?,revision=? WHERE id=?',(repo.encoded(spec),revision,episode['id']))
        await db.execute('INSERT INTO cartoon_versions VALUES (?,?,?,?,CURRENT_TIMESTAMP)',('episode',episode['id'],revision,repo.encoded(spec)))
        jobs=await repo.rows(db,"SELECT id FROM cartoon_jobs WHERE target='episode' AND target_id=? AND kind='assemble' AND status IN ('quoted','queued','preparing')",(episode['id'],))
        for job in jobs:
            await db.execute("UPDATE cartoon_jobs SET status='cancelled',lease_until=0,error='assembly_editing_revised' WHERE id=?",(job['id'],))
            await db.execute('INSERT INTO cartoon_job_events(job_id,event,details) VALUES (?,?,?)',
                             (job['id'],'assembly_editing_revised',repo.encoded({'actor':actor})))
        await db.execute('INSERT INTO cartoon_editing_migrations VALUES (?,?)',(MIGRATION,project_id))
        await db.commit()
    return episode['id']

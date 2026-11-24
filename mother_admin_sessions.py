"""Durable checkpoints for admin tests, which do not have a paid cartoon order."""
from mothers_day import MOTHER_THEMES
import json
import aiosqlite
import database

SCHEMA='''CREATE TABLE IF NOT EXISTS mother_admin_sessions (
    session_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, data TEXT NOT NULL,
    state TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)'''


async def save(user_id,data,state):
    session=data.get('cartoon_session_id')
    if not session or data.get('cartoon_theme') not in MOTHER_THEMES:return
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute(SCHEMA)
        await db.execute('''INSERT INTO mother_admin_sessions(session_id,user_id,data,state) VALUES(?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET data=excluded.data,state=excluded.state,updated_at=CURRENT_TIMESTAMP
            WHERE mother_admin_sessions.user_id=excluded.user_id''',(session,user_id,json.dumps(data,ensure_ascii=False),str(state)))
        await db.commit()


async def latest(user_id):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute(SCHEMA)
        row=await (await db.execute('SELECT data,state FROM mother_admin_sessions WHERE user_id=? '
            'ORDER BY updated_at DESC,rowid DESC LIMIT 1',(user_id,))).fetchone()
        return (json.loads(row[0]),row[1]) if row else None

"""A private, explicit audience and durable delivery state for the repair notice."""
import aiosqlite

import database

NOTICE_KEY = 'generation_fix_2026_11_04'
NOTICE_TEXT = (
    'Здравствуйте! Мы обновили бота для открыток и нейрофото 💌\n\n'
    'Исправили обнаруженные ошибки в меню и отправке сообщений с кнопками '
    'и улучшили обработку сбоев генерации. Приносим извинения, если вам не удалось получить результат.\n\n'
    'Нажмите «Старт», чтобы обновить меню. '
    'Если ваша открытка или фотография так и не пришла, напишите в поддержку — проверим ваш случай и поможем.'
)


def parse_audience(command):
    parts = command.split()
    if not 2 <= len(parts) <= 501 or any(not p.isascii() or not p.isdigit() for p in parts[1:]):
        raise ValueError('Укажите от 1 до 500 MAX ID через пробел после команды.')
    ids = sorted(set(int(p) for p in parts[1:]))
    if any(not 0 < uid < 2**63 for uid in ids):
        raise ValueError('Некорректный MAX ID.')
    return ids


async def prepare_audience(user_ids, *, admin_user_id):
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        await db.execute('UPDATE service_notice_recipients SET selected=0 WHERE notice_key=?', (NOTICE_KEY,))
        eligible = 0
        for uid in user_ids:
            if uid == admin_user_id:
                continue
            async with db.execute('''SELECT 1 FROM users WHERE user_id=? AND is_active=1
                AND NOT EXISTS(SELECT 1 FROM payment_recoveries r WHERE r.user_id=users.user_id)''', (uid,)) as cursor:
                if not await cursor.fetchone():
                    continue
            eligible += 1
            await db.execute('''INSERT INTO service_notice_recipients(notice_key,user_id) VALUES (?,?)
                ON CONFLICT(notice_key,user_id) DO UPDATE SET selected=1,
                status=CASE WHEN status='inactive' THEN 'pending' ELSE status END''', (NOTICE_KEY, uid))
        await db.commit()
    return eligible


async def selected_counts():
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        async with db.execute('''SELECT status,COUNT(*) FROM service_notice_recipients
            WHERE notice_key=? AND selected=1 GROUP BY status''', (NOTICE_KEY,)) as cursor:
            return dict(await cursor.fetchall())


async def pending_recipients():
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        async with db.execute('''SELECT user_id FROM service_notice_recipients
            WHERE notice_key=? AND selected=1 AND status='pending' ORDER BY user_id''', (NOTICE_KEY,)) as cursor:
            return [r[0] for r in await cursor.fetchall()]


async def claim_recipient(user_id):
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        async with db.execute('''SELECT 1 FROM users WHERE user_id=? AND is_active=1
            AND NOT EXISTS(SELECT 1 FROM payment_recoveries r WHERE r.user_id=users.user_id)''', (user_id,)) as cursor:
            active = bool(await cursor.fetchone())
        result = await db.execute('''UPDATE service_notice_recipients SET status=?
            WHERE notice_key=? AND user_id=? AND selected=1 AND status='pending' ''',
            ('sending' if active else 'inactive', NOTICE_KEY, user_id))
        await db.commit()
    return active and result.rowcount == 1


async def finish_recipient(user_id, status):
    if status not in {'sent', 'inactive', 'uncertain', 'pending'}:
        raise ValueError('Неверный статус уведомления.')
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        await db.execute('''UPDATE service_notice_recipients SET status=?,
            sent_at=CASE WHEN ?='sent' THEN CURRENT_TIMESTAMP ELSE sent_at END
            WHERE notice_key=? AND user_id=? AND status='sending' ''', (status,status,NOTICE_KEY,user_id))
        await db.commit()

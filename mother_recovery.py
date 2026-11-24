"""Read retained Mother’s Day media and recover only failed pre-scene alignment."""
import json
from urllib.parse import urlsplit

import aiosqlite
import database
from song_alignment import alignment_source


def urls(data):
    selected = data.get('selected_song_variant') or 1
    image = data.get('original_image_url') or data.get('chosen_avatar')
    audio = data.get(f'song_{selected}_url') or data.get('video_song_url') or data.get('chosen_song_url')
    def valid(url):
        try:
            p = urlsplit(url) if isinstance(url, str) else None
            return url if p and p.scheme == 'https' and p.hostname and not p.username and not p.password else None
        except ValueError:
            return None
    return valid(image), valid(audio)


def alignment_only(data):
    story = data.get('cartoon_kling_story') or {}
    _, audio = urls(data)
    lyrics = data.get('song_review_lyrics') or data.get('video_song_lyrics') or ''
    source = alignment_source(audio, lyrics)
    if (data.get('cartoon_theme') != 'theme_mother' or not audio or story.get('result_url')
            or story.get('alignment_source') == source):
        return False
    if story.get('alignment_attempt_source') == source and story.get('alignment_attempts', 0) >= 2:
        return False
    error = story.get('alignment_error') or {}
    return (story.get('alignment_submitting') == source or error.get('source') == source) and not any(
        (shot.get('image_submitting') and not shot.get('image_url'))
        or (shot.get('video_submitting') and not shot.get('task_id')) for shot in story.get('shots', []))


async def recover_order(order, state):
    """Only undo manual routing for a confirmed paid order blocked by alignment."""
    data = json.loads(order.get('fsm_data') or '{}')
    if order['status'] != 'manual' or not order.get('paid_at') or not alignment_only(data):
        return False
    data['cartoon_video_failed'] = False
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        cursor = await db.execute("UPDATE cartoon_orders SET status='paid',fsm_data=?,fsm_state=? "
            "WHERE order_id=? AND user_id=? AND status='manual' AND fsm_data=? AND paid_at IS NOT NULL",
            (json.dumps(data, ensure_ascii=False), str(state), order['order_id'], order['user_id'], order['fsm_data']))
        await db.commit()
        return cursor.rowcount == 1


async def assets(admin_user_id, order_id=None, live=None):
    if order_id:
        order = await database.get_cartoon_order(order_id)
        if not order or order['theme'] != 'theme_mother':
            raise ValueError('Заказ ко Дню матери не найден.')
        return order_id, json.loads(order.get('fsm_data') or '{}')
    if live and live.get('cartoon_theme') == 'theme_mother' and any(urls(live)):
        return live.get('cartoon_order_id') or live.get('cartoon_session_id') or 'текущий тест', live
    from mother_admin_sessions import latest
    snapshot = await latest(admin_user_id)
    if snapshot and any(urls(snapshot[0])):
        return snapshot[0].get('cartoon_session_id') or 'тест', snapshot[0]
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        row = await (await db.execute("SELECT order_id FROM cartoon_orders WHERE user_id=? AND theme='theme_mother' "
            "ORDER BY rowid DESC LIMIT 1", (admin_user_id,))).fetchone()
    if row:
        return await assets(admin_user_id, row[0])
    raise ValueError('Сохранённых материалов нет. Укажите /mother_assets ID_заказа.')

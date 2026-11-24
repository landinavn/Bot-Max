"""Read retained Mother’s Day media and recover only failed pre-scene alignment."""
import json
from urllib.parse import urlsplit

import aiosqlite
import database
from song_alignment import alignment_source, matched_start, first_lyric


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


def alignment_only(data, *, allow_exhausted=False):
    story = data.get('cartoon_kling_story') or {}
    _, audio = urls(data)
    lyrics = data.get('song_review_lyrics') or data.get('video_song_lyrics') or ''
    source = alignment_source(audio, lyrics)
    if (data.get('cartoon_theme') != 'theme_mother' or not audio or story.get('result_url')
            or story.get('alignment_source') == source):
        return False
    if not allow_exhausted:
        cached = story.get('alignment_transcript') or {}
        if cached.get('audio_url') == audio:
            try:
                matched_start(cached.get('words', []), first_lyric(lyrics)[1])
            except ValueError:
                return False
        elif story.get('alignment_attempt_source') == source and story.get('alignment_attempts', 0) >= 2:
            return False
    error = story.get('alignment_error') or {}
    return (story.get('alignment_submitting') == source or error.get('source') == source) and not any(
        (shot.get('image_submitting') and not shot.get('image_url'))
        or (shot.get('video_submitting') and not shot.get('task_id')) for shot in story.get('shots', []))


def saved_start(data):
    """Return an offset only for the exact selected audio and current approved words."""
    import math
    story = data.get('cartoon_kling_story') or {}
    _, audio = urls(data)
    lyrics = data.get('song_review_lyrics') or data.get('video_song_lyrics') or ''
    start = story.get('audio_start')
    if (data.get('cartoon_theme') == 'theme_mother' and audio and lyrics
            and story.get('alignment_source') == alignment_source(audio, lyrics)
            and isinstance(start, (int, float)) and not isinstance(start, bool)
            and math.isfinite(start) and 0 <= start <= 29):
        return float(start)
    return None


def restore_start_selection(data):
    """A restored selector may have lost its selection; retain the verified audio, never guess another."""
    if data.get('selected_song_variant') in (1, 2):
        return
    story = data.get('cartoon_kling_story') or {}
    source = story.get('alignment_source') or []
    if len(source) != 2:
        return
    matches = [i for i in (1, 2) if data.get(f'song_{i}_url') == source[0]]
    if len(matches) == 1:
        data['selected_song_variant'] = matches[0]


def can_recover_start(data):
    if alignment_only(data):
        return True
    story = data.get('cartoon_kling_story') or {}
    # A stale failure before any visual request can be cleared after verified/manual alignment.
    # Once visual work has begun, its failure must be inspected separately.
    return saved_start(data) is not None and not story.get('result_url') and not any(
        shot.get('image_url') or shot.get('image_submitting') or shot.get('task_id')
        or shot.get('video_url') or shot.get('video_submitting') for shot in story.get('shots', []))


def manual_start(data, seconds):
    import math
    story = data.get('cartoon_kling_story') or {}
    image, audio = urls(data)
    lyrics = data.get('song_review_lyrics') or data.get('video_song_lyrics') or ''
    if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
            or not math.isfinite(seconds) or not 0 <= seconds <= 29):
        raise ValueError('Укажите секунду начала слов от 0 до 29, например /mother_song_start 3.5')
    revising = (data.get('cartoon_theme') == 'theme_mother' and story.get('alignment_method') == 'admin'
        and story.get('alignment_source') == alignment_source(audio, lyrics) and not story.get('result_url')
        and not any((shot.get('image_submitting') and not shot.get('image_url'))
                    or (shot.get('video_submitting') and not shot.get('task_id')) for shot in story.get('shots', [])))
    if not image or not audio or not lyrics or not (alignment_only(data, allow_exhausted=True) or revising):
        raise ValueError('Нет сохранённого сбоя проверки песни. Неизвестные запросы сцен этой командой не сбрасываются.')
    story.update(alignment_source=alignment_source(audio, lyrics), audio_start=float(seconds),
                 alignment_method='admin', alignment_submitting=None, alignment_error=None)
    data['cartoon_video_failed'] = False


async def recover_order(order, state, *, start=None):
    """Only undo manual routing for a confirmed paid order blocked by alignment."""
    data = json.loads(order.get('fsm_data') or '{}')
    restore_start_selection(data)
    if order['status'] != 'manual' or not order.get('paid_at') or not (alignment_only(data, allow_exhausted=True) if start is not None else can_recover_start(data)):
        return False
    if start is not None:
        manual_start(data, start)
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

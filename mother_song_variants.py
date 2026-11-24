"""Two arrangements of exactly the same approved lyrics; durable individual music jobs."""
from mothers_day import mother_event
import asyncio
import hashlib
import json
from urllib.parse import urlsplit

from audio_service import validate_song_arguments, song_audio_url
import song_review

STYLES = {
    '1': ('Тёплое и спокойное', 'warm tender heartfelt performance, relaxed flowing rhythm, gentle accompaniment'),
    '2': ('Чуть веселее', 'warm uplifting performance, slightly more cheerful and lively, moderate rhythmic arrangement, not fast or comic'),
}
POLICY = 'mother-two-arrangements-v1'


class UnknownSongSubmission(ValueError):
    pass


async def prepare(data, *, generator, waiter, checkpoint):
    if not song_review.approved(data, data.get('chosen_song_phrase')):
        raise ValueError('Сначала утвердите текст песни.')
    lyrics, tags, title = (data.get('song_review_lyrics'), data.get('song_review_tags'), data.get('song_review_title'))
    validate_song_arguments(lyrics, tags, title)
    source = hashlib.sha256(json.dumps([POLICY, lyrics, tags, title, STYLES], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    variants = data.get('mother_song_variants')
    if not variants or variants.get('source') != source:
        if variants:
            data.setdefault('mother_song_variants_archive', []).append(variants)
        variants = {'source': source, 'items': {}}
        data['mother_song_variants'] = variants
        # A completed/known job from the previous flow is retained when its words are proven identical.
        old_source = [data.get('chosen_song'), data.get('chosen_song_phrase')]
        if (not variants['items'] and not data.get('mother_song_variants_archive')
                and data.get('video_song_lyrics') == lyrics
                and (data.get('video_song_source') or data.get('video_song_job_source')
                     or data.get('video_song_submitting_source')) == old_source):
            item = {'label': 'Сохранённое исполнение'}
            if data.get('song_1_url') or data.get('video_song_url'):
                item['audio_url'] = data.get('song_1_url') or data['video_song_url']
            elif data.get('video_song_job_id'):
                item['job_id'] = data['video_song_job_id']
            if data.get('video_song_submitting_source') == old_source:
                item['submitting'] = True
            if item.get('audio_url') or item.get('job_id') or item.get('submitting'):
                variants['items']['1'] = item
        # Old public slots must not expose a different song before this source is ready.
        for index in ('1', '2'):
            data[f'song_{index}_url'] = None
            data[f'song_{index}_preview_url'] = None
        data['selected_song_variant'] = None
        data.update(video_song_url=None, chosen_song_url=None, audio_url=None)
        await checkpoint()
    for index, (label, style) in STYLES.items():
        item = variants['items'].setdefault(index, {'label': label})
        if not item.get('audio_url'):
            if not item.get('job_id'):
                if item.get('submitting'):
                    raise UnknownSongSubmission('Результат запроса песни неизвестен. Повторный платный запрос не отправляется; нужна поддержка.')
                effective_tags = tags + ', ' + style + ', vocals start immediately, no instrumental intro'
                validate_song_arguments(lyrics, effective_tags, title)
                item.update(submitting=True, tags=effective_tags)
                await checkpoint()
                try:
                    item['job_id'] = await generator(lyrics, effective_tags, title)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise UnknownSongSubmission('Результат отправки песни неизвестен. Повторный запрос не отправляется; нужна поддержка.') from exc
                if not isinstance(item['job_id'], str) or not item['job_id'].strip():
                    item.pop('job_id', None)
                    await asyncio.shield(checkpoint())
                    raise UnknownSongSubmission('Не получен номер задания песни. Нужна поддержка.')
                await asyncio.shield(checkpoint())
            audio_url = song_audio_url(await waiter(item['job_id']))
            parsed = urlsplit(audio_url)
            if parsed.hostname == 'example.com' or (parsed.hostname or '').endswith('.example.com'):
                raise ValueError('Получена тестовая ссылка вместо песни.')
            item['audio_url'] = audio_url
            await asyncio.shield(checkpoint())
        if data.get('song_review_user') and item.get('job_id'):
            from mother_stats import record
            await record(data['song_review_user'], mother_event(data,'mother_video_song_ready'), item['job_id'])
        data[f'song_{index}_url'] = item['audio_url']
        data[f'song_{index}_phrase'] = data['chosen_song_phrase']
        await checkpoint()
    return variants

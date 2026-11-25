"""Versioned production rules. Unmarked historical scenes retain their old timing."""
import copy
import json
import math

VERSION = 'single-speaker-5s-v1'
SECONDS = 5.0
MAX_SPEECH = 4.5
TOLERANCE = 1 / 30 + 0.005


def strict(spec):
    return spec.get('production_rules') == VERSION


def normalize(spec):
    result = copy.deepcopy(spec)
    result.update(production_rules=VERSION, duration=5, scene_duration_seconds=5,
                  max_speakers_per_scene=1)
    lines = result.get('dialogue', [])
    speakers = {line.get('character_id') for line in lines}
    if len(lines) > 1 or len(speakers) > 1:
        raise ValueError('Одна сцена — одна реплика одного персонажа. Разделите диалог на отдельные сцены.')
    speaker = next(iter(speakers), None)
    result['speaker_id'] = speaker
    if speaker:
        if speaker not in result.get('characters', []):
            raise ValueError('Говорящий должен присутствовать в characters.')
        result['characters'] = [speaker]
        result['video_provider'] = ('kling_avatar' if result.get('kling_audio_required') else 'openai' if result.get('openai_audio_sync_required') else 'heygen')
        result['camera'] = result.get('camera_direction') or ('Один средний план по пояс: только говорящий персонаж, обе руки, телефон и край стола.'
                            if result.get('framing')=='medium' else
                            'Один крупный или средний план только говорящего персонажа. Остальные за кадром.')
    else:
        result['video_provider'] = 'heygen_video'
    validate(result)
    return result


def split_dialogue(spec):
    """Separate ordered utterances before insertion; never rewrite an old scene."""
    lines = spec.get('dialogue', [])
    if len(lines) <= 1:
        return [normalize(spec)]
    result = []
    for index, line in enumerate(lines, 1):
        shot = copy.deepcopy(spec)
        shot['dialogue'] = [line]
        shot['title'] = f"{spec.get('title', 'Диалог')} · реплика {index}"
        result.append(normalize(shot))
    return result


def validate(spec):
    if not strict(spec):
        return
    if spec.get('duration') != 5 or spec.get('scene_duration_seconds') != 5:
        raise ValueError('Новая сцена должна длиться ровно 5 секунд.')
    if spec.get('max_speakers_per_scene') != 1:
        raise ValueError('Один кадр — максимум один голос.')
    if spec.get('camera_direction') is not None and (not isinstance(spec['camera_direction'],str) or not spec['camera_direction'].strip() or len(spec['camera_direction'])>1500):
        raise ValueError('Укажите один понятный ракурс в camera_direction, до 1500 символов.')
    lines = spec.get('dialogue', [])
    if not isinstance(lines, list) or len(lines) > 1:
        raise ValueError('Разделите реплики на отдельные пятисекундные сцены.')
    speaker = spec.get('speaker_id')
    if lines:
        if not speaker or lines[0].get('character_id') != speaker or spec.get('characters') != [speaker]:
            raise ValueError('В говорящей сцене должен быть только её speaker_id.')
        text = lines[0].get('text')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Укажите непустую реплику говорящего.')
        if spec.get('video_provider') not in ('heygen','openai','kling_avatar') or (spec.get('video_provider')=='openai' and not spec.get('openai_audio_sync_required')) or (spec.get('video_provider')=='kling_avatar' and not spec.get('kling_audio_required')):
            raise ValueError('Для говорящей сцены нужен поддерживаемый маршрут с утверждённой озвучкой.')
    elif speaker is not None:
        raise ValueError('У немой сцены speaker_id должен быть null.')


def audio_seconds(value, offset=0):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError('Не измерена фактическая длительность озвучки.')
    if value > MAX_SPEECH or offset < 0 or offset + value > SECONDS:
        raise ValueError(f'Реплика длится {value:.2f} с: максимум 4,50 с, с паузой — не более 5 с. '
                         'Сократите текст или разделите реплику на две сцены и подтвердите новую стоимость. '
                         'Видео не запущено; аудио сохранено.')
    return value


def video_seconds(value, count=1):
    if not math.isfinite(value) or abs(value - count * SECONDS) > TOLERANCE:
        raise ValueError(f'Нужен ролик {count * SECONDS:.2f} с; получено {value:.3f} с. Результат не принят.')


def speech_metadata(snap, seconds):
    profiles = snap['quote']['speech_profiles']
    return dict(production_rules=VERSION, audio_duration_seconds=seconds,
                speaker_id=snap['spec']['speaker_id'], voice_profile=profiles[snap['spec']['speaker_id']],
                text=snap['spec']['dialogue'][0]['text'])


def validate_audio_asset(snap):
    """Reject stale/wrong-voice audio before spending money on video."""
    if not strict(snap['spec']) or not snap['spec'].get('dialogue'):
        return
    asset = snap.get('assets', {}).get('speech')
    if not asset:
        raise ValueError('Сначала создайте и проверьте озвучку. Видео ещё не запускается.')
    metadata = json.loads(asset.get('metadata') or '{}')
    audio_seconds(metadata.get('audio_duration_seconds'))
    if metadata.get('text') != snap['spec']['dialogue'][0]['text'] or metadata.get('speaker_id') != snap['spec']['speaker_id']:
        raise ValueError('Озвучка относится к другой реплике или персонажу.')
    profile = snap.get('quote', {}).get('speech_profiles', {}).get(snap['spec']['speaker_id'])
    if not profile or profile != metadata.get('voice_profile'):
        raise ValueError('Голос или скорость изменились. Создайте новую озвучку с сохранёнными настройками.')


def shorter_suggestion(text):
    """A proposal only: never silently change meaning or submit another TTS request."""
    proposals = {
        'Мам! Я придумала, как увидеть твою мечту!': 'Мам! Давай увидим твою мечту!',
        'Маш, как увидеть то, чего ещё нет?': 'Маш, как увидеть мечту?',
        'Загружаешь фото, пишешь желание — и готово!': 'Фото, желание — и готово!',
        'Хочу на море! И прямо сейчас!': 'Хочу на море! Сейчас!',
        'Это же я! Как будто уже на море!': 'Это я! Уже на море!',
        'А можно мне на море, где никто не трогает?': 'А мне море, где никто не трогает!',
        'Кажется, моя мечта стала немного ближе…': 'Моя мечта стала ближе…',
    }
    return proposals.get(text, 'Сократите реплику, сохранив смысл, или разделите её на две сцены по 5 секунд.')


def validate_video_asset(snap):
    if not strict(snap['spec']): return
    video=snap.get('assets',{}).get('video')
    if not video or not video.get('approved'):
        raise ValueError('Сначала просмотрите и утвердите пятисекундное видео сцены.')
    metadata=json.loads(video.get('metadata') or '{}')
    video_seconds(float(metadata.get('duration_seconds',0)))
    if snap['spec'].get('tail_cutaway_scene_id'):
        if (metadata.get('tail_policy')!='approved-dream-card-no-freeze'
                or metadata.get('cutaway_id')!=(snap.get('cutaway_reference') or {}).get('id')):
            raise ValueError('В этой версии нужен проверенный немой план карты после речи; неподвижный хвост не допускается.')
    if metadata.get('image_id')!=snap.get('assets',{}).get('image',{}).get('id'):
        raise ValueError('Видео снято по другому изображению. Выберите соответствующие исходники или перегенерируйте сцену.')
    if metadata.get('speech_id')!=snap.get('assets',{}).get('speech',{}).get('id'):
        raise ValueError('Видео снято с другой озвучкой. Утвердите соответствующее видео новой реплики.')

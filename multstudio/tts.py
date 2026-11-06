"""SpeechKit voice profiles; preserve visual specs and execute frozen quote inputs."""
import math

MODEL = 'yandex-speechkit-v1'
VOICES = {'alena', 'filipp', 'ermil', 'jane', 'omazh', 'zahar', 'masha'}
MASHA_PROFILE = dict(voice='masha', emotion='good', speed=1.0, pitch_shift=80.0)
V3_MODEL = 'yandex-speechkit-v3'
EMOTIONS = {'neutral', 'good', 'evil'}
LEGACY_VOICES = {
    'nova': ('alena', 'neutral'), 'onyx': ('filipp', 'neutral'),
    'coral': ('alena', 'good'), 'alloy': ('alena', 'neutral'),
    'shimmer': ('alena', 'neutral'), 'echo': ('filipp', 'neutral'),
    'fable': ('filipp', 'neutral'), 'ash': ('filipp', 'neutral'),
    'sage': ('alena', 'neutral'),
}

DEFAULT_PROFILES = {
    'Аня': dict(voice='alena', old_emotion='neutral', emotion='good', speed=1.05),
    'Дима': dict(voice='filipp', old_emotion='neutral', emotion='neutral', speed=0.95),
    'Маша': dict(voice='alena', old_emotion='good', emotion='good', speed=1.1),
}


def normalize_speed(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        speed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Скорость SpeechKit должна быть числом от 0.1 до 3.0.') from None
    if not math.isfinite(speed) or not 0.1 <= speed <= 3.0:
        raise ValueError('Скорость SpeechKit должна быть числом от 0.1 до 3.0.')
    return speed


def voice_settings(spec):
    voice = spec.get('voice')
    if not isinstance(voice, str):
        raise ValueError('Укажите voice SpeechKit строкой.')
    voice, default_emotion = LEGACY_VOICES.get(voice, (voice, 'good' if voice=='masha' else 'neutral'))
    emotion = spec.get('emotion', default_emotion)
    if voice not in VOICES:
        raise ValueError('Укажите голос SpeechKit: ' + ', '.join(sorted(VOICES)))
    allowed = {'good', 'strict', 'friendly'} if voice == 'masha' else EMOTIONS
    if not isinstance(emotion, str) or emotion not in allowed:
        raise ValueError('Для masha: good, strict, friendly; для v1: neutral, good, evil.')
    return voice, emotion


def speech_settings(spec, name=None):
    """Resolve old seed defaults without changing image/video identity in the DB."""
    voice, emotion = voice_settings(spec)
    default = DEFAULT_PROFILES.get(name or spec.get('name'))
    speed = spec.get('speed', default['speed'] if default else 1.0)
    if 'speed' not in spec and default and voice == default['voice']:
        if 'emotion' not in spec or emotion == default['old_emotion']:
            emotion = default['emotion']
    profile = dict(voice=voice, emotion=emotion, speed=normalize_speed(speed))
    if voice == 'masha':
        value = spec.get('pitch_shift', 0)
        try:
            if isinstance(value, bool): raise ValueError
            pitch = float(value)
            if not math.isfinite(pitch) or not -1000 <= pitch <= 1000: raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise ValueError('Высота голоса pitch_shift должна быть числом от -1000 до 1000 Гц.') from None
        profile['pitch_shift'] = pitch
    return profile


def validate_speech_snapshot(snap):
    if snap.get('quote', {}).get('model') not in (MODEL, V3_MODEL, MODEL+'+v3'):
        raise ValueError('Задание озвучки подтверждено для старого провайдера. '
                         'Создайте новую оценку SpeechKit и подтвердите стоимость.')
    cast = {c['id']: c for c in snap.get('cast', [])}
    profiles = snap.get('quote', {}).get('speech_profiles')
    if not isinstance(profiles, dict):
        raise ValueError('Обновились эмоции и скорость озвучки. Создайте новую оценку и подтвердите параметры.')
    for line in snap['spec'].get('dialogue', []):
        if line.get('character_id') not in cast:
            raise ValueError('Говорящий персонаж отсутствует в сцене.')
        profile = profiles.get(line['character_id'])
        if not isinstance(profile, dict) or not {'voice','emotion','speed'} <= profile.keys():
            raise ValueError('Не сохранены параметры голоса для реплики. Создайте новую оценку.')
        if profile['voice']=='masha' and 'pitch_shift' not in profile:
            raise ValueError('Не сохранена высота голоса Маши. Создайте новую оценку.')
        resolved = speech_settings(profile)
        if resolved['voice']=='masha' and snap['quote']['model'] not in (V3_MODEL, MODEL+'+v3'):
            raise ValueError('Голос masha требует новой оценки SpeechKit v3.')
        text = line.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise ValueError('Реплика SpeechKit должна содержать от 1 до 5000 символов.')

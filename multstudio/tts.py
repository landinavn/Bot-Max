"""SpeechKit v1 configuration and compatibility for saved character voices."""

MODEL = 'yandex-speechkit-v1'
VOICES = {'alena', 'filipp', 'ermil', 'jane', 'omazh', 'zahar'}
EMOTIONS = {'neutral', 'good', 'evil'}
LEGACY_VOICES = {
    'nova': ('alena', 'neutral'), 'onyx': ('filipp', 'neutral'),
    'coral': ('alena', 'good'), 'alloy': ('alena', 'neutral'),
    'shimmer': ('alena', 'neutral'), 'echo': ('filipp', 'neutral'),
    'fable': ('filipp', 'neutral'), 'ash': ('filipp', 'neutral'),
    'sage': ('alena', 'neutral'),
}


def voice_settings(spec):
    voice = spec.get('voice')
    if not isinstance(voice, str):
        raise ValueError('Укажите voice SpeechKit v1 строкой.')
    voice, default_emotion = LEGACY_VOICES.get(voice, (voice, 'neutral'))
    emotion = spec.get('emotion', default_emotion)
    if voice not in VOICES:
        raise ValueError('Укажите голос SpeechKit v1: ' + ', '.join(sorted(VOICES)))
    if not isinstance(emotion, str) or emotion not in EMOTIONS:
        raise ValueError('Эмоция SpeechKit v1: neutral, good или evil.')
    return voice, emotion


def validate_speech_snapshot(snap):
    if snap.get('quote', {}).get('model') != MODEL:
        raise ValueError('Задание озвучки подтверждено для старого провайдера. '
                         'Создайте новую оценку SpeechKit и подтвердите стоимость.')
    cast = {c['id']: c for c in snap.get('cast', [])}
    for line in snap['spec'].get('dialogue', []):
        if line.get('character_id') not in cast:
            raise ValueError('Говорящий персонаж отсутствует в сцене.')
        voice_settings(cast[line['character_id']]['spec'])
        text = line.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise ValueError('Реплика SpeechKit должна содержать от 1 до 5000 символов.')

"""Independent studio settings; never change customer models, prices or credits."""
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path


def money(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Введите сумму USD числом, например 10.50.') from None
    if not number.is_finite() or number < 0 or number > 100000:
        raise ValueError('Сумма USD должна быть от 0 до 100000.')
    return int((number * 1000000).to_integral_value(rounding=ROUND_CEILING))


def dollars(value):
    return f'{Decimal(value) / 1000000:.6f} USD'


@dataclass(frozen=True)
class Settings:
    root: Path
    image_model: str = ''
    image_quality: str = 'high'
    kling_key: str = field(default='', repr=False)
    tts_key: str = field(default='', repr=False)
    image_price: int = 0
    video_price: int = 0  # Estimate per 5 seconds; must be configured by owner.
    speech_price: int = 0  # Estimate per 1000 characters.
    concurrency: int = 1
    enabled: bool = False

    @classmethod
    def from_config(cls, config):
        return cls(root=Path(os.getenv('STUDIO_FILES_DIR', str(Path(__file__).resolve().parents[1] / 'studio_files'))),
                   image_model=config.image_model, image_quality=config.image_quality,
                   kling_key=os.getenv('STUDIO_KLING_API_KEY') or config.piapi_key or '',
                   tts_key=os.getenv('STUDIO_TTS_API_KEY') or config.image_api_key or '',
                   image_price=money(os.getenv('STUDIO_IMAGE_USD', '0')),
                   video_price=money(os.getenv('STUDIO_KLING_5S_USD', '0')),
                   speech_price=money(os.getenv('STUDIO_TTS_1000_CHARS_USD', '0')),
                   concurrency=max(1, min(3, int(os.getenv('STUDIO_CONCURRENCY', '1')))),
                   enabled=os.getenv('STUDIO_ENABLED', '0') == '1')

    def quote(self, kind, snapshot):
        duration = snapshot.get('spec', {}).get('duration', 0)
        if kind == 'image':
            price, model, size = self.image_price, self.image_model, '1024x1536'
        elif kind == 'video':
            duration = 5 if duration <= 5 else 10
            price, model, size = self.video_price * (duration // 5), 'kling/3.0/std', '9:16; итог 1080x1920'
        elif kind == 'speech':
            chars = sum(len(x['text']) for x in snapshot['spec'].get('dialogue', []))
            price = (self.speech_price * chars + 999) // 1000
            model, size = 'gpt-4o-mini-tts', 'MP3; естественная речь'
        elif kind == 'assemble':
            price, model, size = 0, 'FFmpeg/local', '1080x1920 / 30 FPS / H.264 + AAC'
        else:
            raise ValueError('Неизвестное задание.')
        if kind != 'assemble' and (price <= 0 or not model):
            raise ValueError('Сначала настройте модель и ненулевую оценку стоимости STUDIO_*_USD.')
        return dict(estimate=price, model=model, resolution=size, duration=duration,
                    image_quality=self.image_quality, currency='USD')

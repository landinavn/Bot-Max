from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthSettings:
    secret: bytes = field(repr=False)
    environment: str = 'production'
    service_name: str = 'Bot-Max'
    code_digits: int = 6
    code_ttl: int = 600
    resend_interval: int = 60
    challenge_attempts: int = 5
    rate_window: int = 3600
    email_requests: int = 5
    source_requests: int = 30
    global_requests: int = 1000
    email_attempts: int = 20
    source_attempts: int = 100
    global_attempts: int = 5000
    delivery_timeout: float = 10
    session_lifetime: int = 30 * 86400
    session_idle: int = 7 * 86400
    session_touch: int = 300
    retention: int = 86400

    def __post_init__(self):
        if not isinstance(self.secret, bytes) or len(self.secret) < 32:
            raise ValueError('Нужен отдельный секрет авторизации не короче 32 байт.')
        if self.environment not in ('production', 'development', 'test'):
            raise ValueError('Неизвестный режим авторизации.')
        if not isinstance(self.service_name, str) or not 1 <= len(self.service_name) <= 100 or any(ord(c)<32 for c in self.service_name):
            raise ValueError('Некорректное название сервиса.')
        integers = ('code_digits','code_ttl','resend_interval','challenge_attempts','rate_window','email_requests',
                    'source_requests','global_requests','email_attempts','source_attempts','global_attempts',
                    'session_lifetime','session_idle','session_touch','retention')
        if any(type(getattr(self, k)) is not int or getattr(self, k) <= 0 for k in integers):
            raise ValueError('Лимиты авторизации должны быть положительными целыми числами.')
        if (not 6 <= self.code_digits <= 8 or self.session_touch >= self.session_idle
                or self.resend_interval > self.rate_window):
            raise ValueError('Некорректная длина кода или частота обновления сессии.')
        if (isinstance(self.delivery_timeout, bool) or not isinstance(self.delivery_timeout,(int,float))
                or not 0 < self.delivery_timeout <= 60):
            raise ValueError('Таймаут доставки должен быть от 0 до 60 секунд.')

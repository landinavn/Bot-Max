class AuthError(Exception):
    """Public errors never include addresses, codes, tokens or underlying exceptions."""


class InvalidCode(AuthError):
    def __init__(self):
        super().__init__('Код недействителен или истёк. Запросите новый код.')


class RateLimited(AuthError):
    def __init__(self):
        super().__init__('Слишком много попыток. Попробуйте позднее.')


class InvalidSession(AuthError):
    def __init__(self):
        super().__init__('Сессия недействительна. Войдите снова.')


class DeliveryFailed(Exception):
    """Adapter can prove the message was NOT accepted; other failures are uncertain."""

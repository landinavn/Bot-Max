"""Email ports: no connections or configuration reads at import time."""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class LoginEmail:
    recipient: str = field(repr=False)
    subject: str
    body: str = field(repr=False)
    challenge_id: str


class EmailSender(Protocol):
    async def send(self, message: LoginEmail) -> None:
        """Return only after acceptance; DeliveryFailed means definitely not accepted."""
        ...


class SMTPTransport(EmailSender, Protocol):
    """An injected SMTP adapter must implement the same acceptance/error contract."""


class FakeEmailSender:
    test_only = True

    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def login_email(email, code, challenge, settings):
    minutes = settings.code_ttl / 60
    ttl = str(int(minutes)) if minutes.is_integer() else f'{settings.code_ttl} секунд'
    ttl = ttl+' минут' if minutes.is_integer() else ttl
    return LoginEmail(email, 'Код для входа',
        f'{settings.service_name}\n\nВаш код для входа: {code}.\n\n'
        f'Код действителен {ttl}.\n\nЕсли вы не запрашивали код, проигнорируйте это письмо.\n\n'
        'Никогда не передавайте код другим людям.', challenge)

"""Values crossing a privileged service boundary; never proof supplied by an HTTP client."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from uuid import UUID

from .errors import InvalidIdentity


def account_id(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise InvalidIdentity('Нужен канонический UUID аккаунта.') from None
    return value


def max_subject(value):
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise InvalidIdentity('MAX ID должен быть положительным 64-битным целым числом.')
    return str(value)


def normalize_email(value):
    # Preserve the potentially case-sensitive local part, dots and plus aliases.
    if not isinstance(value, str) or not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InvalidIdentity('На этом этапе поддерживаются только обычные ASCII email-адреса.')
    value = value.strip(' ')
    if len(value) > 254 or value.count('@') != 1:
        raise InvalidIdentity('Некорректный email.')
    local, domain = value.split('@')
    if (not 1 <= len(local) <= 64 or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\-]+", local)
            or local.startswith('.') or local.endswith('.') or '..' in local):
        raise InvalidIdentity('Некорректный email.')
    labels = domain.split('.')
    if len(labels) < 2 or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?', label) for label in labels):
        raise InvalidIdentity('Некорректный домен email.')
    return local + '@' + domain.lower()


def identity(provider, subject):
    if not isinstance(provider, str) or not re.fullmatch(r'[a-z][a-z0-9_]{1,31}', provider):
        raise InvalidIdentity('Некорректный provider.')
    if provider == 'max':
        if not isinstance(subject, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', subject):
            raise InvalidIdentity('Некорректный MAX subject.')
        return provider, max_subject(int(subject))
    if provider == 'email':
        return provider, normalize_email(subject)
    if (not isinstance(subject, str) or not 1 <= len(subject) <= 255
            or subject != subject.strip() or any(ord(c) < 32 or ord(c) == 127 for c in subject)):
        raise InvalidIdentity('Некорректный subject.')
    return provider, subject


def verified_time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidIdentity('Подтверждение требует даты с часовым поясом.')
    if value > datetime.now(timezone.utc):
        raise InvalidIdentity('Дата подтверждения не может быть в будущем.')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


@dataclass(frozen=True)
class VerifiedIdentity:
    """Trusted assertion AFTER ownership verification, not an email/MAX verifier.

    Only an authenticated internal service may construct this object. It must
    separately authorize the target account before attachment. Never deserialize
    this type from user input. The challenge/approval flows are outside stage 1.1.
    """
    provider: str
    provider_subject: str = field(repr=False)
    verified_at: datetime

    def __post_init__(self):
        provider, subject = identity(self.provider, self.provider_subject)
        verified_time(self.verified_at)
        object.__setattr__(self, 'provider', provider)
        object.__setattr__(self, 'provider_subject', subject)


@dataclass(frozen=True)
class Account:
    id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AccountIdentity:
    id: str
    account_id: str
    provider: str
    provider_subject: str = field(repr=False)
    verified_at: str
    created_at: str

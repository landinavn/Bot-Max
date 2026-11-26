from dataclasses import dataclass, field
from accounts.models import Account


@dataclass(frozen=True)
class CodeRequest:
    challenge_id: str
    expires_in: int
    message: str = 'Если адрес указан верно, на него отправлен код для входа.'


@dataclass(frozen=True)
class LoginResult:
    account: Account
    session_id: str
    token: str = field(repr=False)
    expires_at: int

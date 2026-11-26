"""Internal assertions are constructed by trusted server adapters, never browser input.

These dataclasses are NOT cryptographic payment verification or authorization.
No HTTP deserializer or payment adapter is connected in this stage.
"""
from dataclasses import dataclass
import re
from .errors import InvalidOperation


def token(value, *, optional=False):
    if optional and value is None: return value
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}', value):
        raise InvalidOperation('Требуется технический идентификатор.')
    return value


def integer(value, *, minimum=1, maximum=10**9):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidOperation('Требуется целое число в допустимом диапазоне.')
    return value


@dataclass(frozen=True)
class GrantSpec:
    kind: str
    quantity: int
    products: tuple[str, ...]
    bucket: str = 'paid'
    package_ref: str | None = None
    order_ref: str | None = None
    expires_at: int | None = None


@dataclass(frozen=True)
class VerifiedGrantEvent:
    account_id: str
    source: str
    external_id: str
    verified_at: int
    grants: tuple[GrantSpec, ...]
    # Only these technical metadata keys are accepted by the repository.
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class VerifiedOutcome:
    outcome: str  # success | failed | not_submitted; unknown is a separate hold state
    evidence_ref: str


@dataclass(frozen=True)
class Reservation:
    id: str
    account_id: str
    kind: str
    product: str
    quantity: int
    order_ref: str | None
    generation_ref: str
    state: str
    created_at: int


@dataclass(frozen=True)
class PackageView:
    """Allowlisted future API response, no legacy/payment/provider identifiers."""
    package_id: str
    kind: str
    products: tuple[str, ...]
    source: str
    available: int
    reserved: int
    consumed: int
    expired: int
    status: str
    expires_at: int | None

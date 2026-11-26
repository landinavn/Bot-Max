from dataclasses import dataclass, field


@dataclass(frozen=True)
class Operation:
    operation_id: str
    status: str
    expires_at: int
    max_confirmed: bool
    max_hint: str | None = None


@dataclass(frozen=True)
class Started:
    operation: Operation
    code: str = field(repr=False)


@dataclass(frozen=True)
class MaxClaim(Operation):
    web_hint: str = ""

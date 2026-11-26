"""Internal use-case boundary; no transports or live payment integrations.

Authorization and trusted event construction belong to the future server adapter.
Do not expose repository methods as public HTTP endpoints.
"""
from .repository import EntitlementRepository


class EntitlementService(EntitlementRepository):
    pass

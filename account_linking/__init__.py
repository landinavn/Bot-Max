"""Two-sided identity linking. No configuration, migrations or I/O at import."""
from .service import LinkingService
from .settings import LinkingSettings

__all__ = ['LinkingService', 'LinkingSettings']

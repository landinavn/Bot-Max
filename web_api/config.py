"""Explicit web-only settings; never imports the MAX configuration."""
from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from auth.settings import AuthSettings


@dataclass(frozen=True)
class APISettings:
    database: Path
    secret: bytes = field(repr=False)
    environment: str = 'production'
    origin: str = 'https://app.wish-bot.ru'
    development_origins: tuple[str, ...] = ()
    trusted_proxies: tuple[str, ...] = ()
    secure_cookies: bool = True
    body_limit: int = 4096
    database_timeout: float = 1

    def __post_init__(self):
        object.__setattr__(self, 'database', Path(self.database).expanduser().resolve())
        AuthSettings(secret=self.secret, environment=self.environment)
        for origin in self.origins:
            u = urlsplit(origin)
            if (u.scheme not in ('http','https') or not u.hostname or u.username or u.password
                    or u.path or u.query or u.fragment or u.netloc != ('['+u.hostname+']' if ':' in u.hostname else u.hostname) + (':'+str(u.port) if u.port else '')):
                raise ValueError('Invalid API origin')
        if self.environment == 'production' and (self.origin != 'https://app.wish-bot.ru'
                or self.development_origins or not self.secure_cookies):
            raise ValueError('Unsafe production browser settings')
        if type(self.body_limit) is not int or not 256 <= self.body_limit <= 16384:
            raise ValueError('Invalid body limit')
        if isinstance(self.database_timeout,bool) or not 0 < self.database_timeout <= 5:
            raise ValueError('Invalid SQLite timeout')
        for value in self.trusted_proxies:
            network = ipaddress.ip_network(value, strict=True)
            if network.prefixlen != network.max_prefixlen or not network.network_address.is_loopback:
                raise ValueError('Only explicit loopback proxy addresses are supported')
        if not self.secure_cookies and self.environment != 'test':
            if any(not ipaddress.ip_address(urlsplit(o).hostname).is_loopback for o in self.origins):
                raise ValueError('HTTP cookies allowed only on explicit local development origins')

    @property
    def origins(self):
        return (self.origin, *self.development_origins)

    @property
    def session_cookie(self):
        return '__Host-wish_session' if self.secure_cookies else 'wish_dev_session'

    @property
    def preauth_cookie(self):
        return '__Host-wish_preauth' if self.secure_cookies else 'wish_dev_preauth'

    @classmethod
    def from_env(cls):
        return cls(database=Path(os.environ['WEB_API_DATABASE']),secret=os.environ['WEB_API_AUTH_SECRET'].encode(),
            environment=os.getenv('WEB_API_ENV','production'),origin=os.getenv('WEB_API_ORIGIN','https://app.wish-bot.ru'),
            development_origins=tuple(filter(None,os.getenv('WEB_API_DEV_ORIGINS','').split(','))),
            trusted_proxies=tuple(filter(None,os.getenv('WEB_API_TRUSTED_PROXIES','').split(','))),
            secure_cookies=os.getenv('WEB_API_SECURE_COOKIES','true')=='true')

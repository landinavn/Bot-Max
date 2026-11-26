from dataclasses import dataclass, field


@dataclass(frozen=True)
class LinkingSettings:
    secret: bytes = field(repr=False)
    code_ttl: int = 600
    fresh_auth: int = 300
    rate_window: int = 3600
    account_starts: int = 5
    source_starts: int = 20
    global_starts: int = 1000
    max_attempts: int = 20
    global_attempts: int = 5000
    operation_attempts: int = 5
    account_actions: int = 100
    source_actions: int = 200
    global_actions: int = 10000

    def __post_init__(self):
        if not isinstance(self.secret,bytes) or len(self.secret)<32:
            raise ValueError('A dedicated account-linking secret of at least 32 bytes is required.')
        for name in self.__dataclass_fields__:
            if name!='secret' and (type(getattr(self,name)) is not int or getattr(self,name)<=0):
                raise ValueError('Linking limits must be positive integers.')
        if self.code_ttl>600 or self.fresh_auth>300:
            raise ValueError('Linking TTL/fresh authentication cannot exceed 10/5 minutes.')

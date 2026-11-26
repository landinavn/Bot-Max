"""Controlled failures for the internal account store (no identity values in messages)."""

class AccountError(Exception):
    pass


class InvalidIdentity(AccountError, ValueError):
    pass


class IdentityConflict(AccountError):
    pass


class AccountNotFound(AccountError):
    pass


class LegacyMaxUserNotFound(AccountError):
    pass


class LegacySchemaError(AccountError):
    pass


class AccountStoreBusy(AccountError):
    pass


class AccountStorageError(AccountError):
    pass


class SchemaCompatibilityError(AccountError):
    pass

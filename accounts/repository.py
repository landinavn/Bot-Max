"""Privileged account repository, independent of MAX transports and legacy balances."""
from datetime import datetime, timezone
from uuid import uuid4

from .errors import AccountNotFound, IdentityConflict, InvalidIdentity, LegacyMaxUserNotFound, LegacySchemaError
from .models import Account, AccountIdentity, VerifiedIdentity, account_id, identity, max_subject, normalize_email, verified_time
from .storage import connect, connection_timeout, database_path, transaction


async def _one(db, sql, args=()):
    cursor = await db.execute(sql, args)
    row = await cursor.fetchone()
    return dict(row) if row else None


class AccountRepository:
    """All calls are internal. Authentication/authorization belong to the calling service.

    Construction does no I/O. The database path is always explicit. Migration is
    explicit too: this repository never creates/migrates a database automatically.
    """
    def __init__(self, path, *, timeout=5):
        self.path = database_path(path)
        self.timeout = connection_timeout(timeout)

    async def _new(self, db):
        key = str(uuid4())
        await db.execute('INSERT INTO accounts(id) VALUES(?)', (key,))
        return Account(**await _one(db, 'SELECT * FROM accounts WHERE id=?', (key,)))

    async def create_account(self):
        async with connect(self.path, self.timeout) as db:
            async with transaction(db):
                return await self._new(db)

    async def get_account(self, key):
        key = account_id(key)
        async with connect(self.path, self.timeout, readonly=True) as db:
            row = await _one(db, 'SELECT * FROM accounts WHERE id=?', (key,))
            return Account(**row) if row else None

    async def get_account_by_identity(self, provider, provider_subject):
        provider, subject = identity(provider, provider_subject)
        async with connect(self.path, self.timeout, readonly=True) as db:
            row = await _one(db, '''SELECT a.* FROM accounts a JOIN account_identities i ON a.id=i.account_id
                WHERE i.provider=? AND i.provider_subject=?''', (provider, subject))
            return Account(**row) if row else None

    async def list_account_identities(self, key):
        key = account_id(key)
        async with connect(self.path, self.timeout, readonly=True) as db:
            cursor = await db.execute('SELECT * FROM account_identities WHERE account_id=? ORDER BY created_at,id', (key,))
            return [AccountIdentity(**dict(row)) for row in await cursor.fetchall()]

    async def _legacy_max(self, db, subject):
        table = await _one(db, "SELECT type FROM sqlite_master WHERE name='users'")
        if not table or table['type'] != 'table':
            raise LegacySchemaError('Для MAX требуется существующая legacy-таблица users.')
        cursor = await db.execute('PRAGMA table_info(users)')
        columns = {r['name']: r for r in await cursor.fetchall()}
        column = columns.get('user_id')
        if (not column or column['type'].upper() != 'INTEGER' or not column['pk']
                or sum(bool(c['pk']) for c in columns.values()) != 1):
            raise LegacySchemaError('Legacy users.user_id должен быть INTEGER PRIMARY KEY.')
        if not await _one(db, 'SELECT user_id FROM users WHERE user_id=?', (int(subject),)):
            raise LegacyMaxUserNotFound('Пользователь MAX должен быть явно зарегистрирован старым сценарием бота.')

    async def _insert_identity(self, db, key, verified):
        if not isinstance(verified, VerifiedIdentity):
            raise InvalidIdentity('Нужно подтверждение доверенного внутреннего сервиса.')
        provider, subject = identity(verified.provider, verified.provider_subject)
        stamp = verified_time(verified.verified_at)
        if provider == 'max':
            await self._legacy_max(db, subject)
        found = await _one(db, 'SELECT * FROM account_identities WHERE provider=? AND provider_subject=?', (provider, subject))
        if found:
            if found['account_id'] != key:
                raise IdentityConflict('Идентичность уже принадлежит другому аккаунту. Автоматического объединения нет.')
            return AccountIdentity(**found)
        if provider == 'max' and await _one(db, "SELECT id FROM account_identities WHERE account_id=? AND provider='max'", (key,)):
            raise IdentityConflict('Аккаунт уже имеет другую MAX-идентичность.')
        ident = str(uuid4())
        await db.execute('''INSERT INTO account_identities(id,account_id,provider,provider_subject,verified_at)
                            VALUES(?,?,?,?,?)''', (ident, key, provider, subject, stamp))
        await db.execute("UPDATE accounts SET updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (key,))
        return AccountIdentity(**await _one(db, 'SELECT * FROM account_identities WHERE id=?', (ident,)))

    async def get_or_create_max_account(self, max_user_id):
        """Trusted legacy adapter only: the ID must come from an authenticated MAX event.

        This creates a mapping, never authenticates or attaches a web session.
        It does not create users or grant free trials, credits or paid rights.
        """
        subject = max_subject(max_user_id)
        async with connect(self.path, self.timeout) as db:
            async with transaction(db):
                await self._legacy_max(db, subject)
                row = await _one(db, '''SELECT a.* FROM accounts a JOIN account_identities i ON a.id=i.account_id
                    WHERE i.provider='max' AND i.provider_subject=?''', (subject,))
                if row:
                    return Account(**row)
                account = await self._new(db)
                await self._insert_identity(db, account.id, VerifiedIdentity('max', subject, datetime.now(timezone.utc)))
                return Account(**await _one(db, 'SELECT * FROM accounts WHERE id=?', (account.id,)))

    async def create_account_with_verified_email(self, email, *, verification):
        """Caller must first verify ownership; a raw user-supplied email is insufficient.

        `verification` is a trusted internal assertion, NOT an API request model.
        Existing identities raise conflict; this method never silently merges/logs in.
        """
        email = normalize_email(email)
        if not isinstance(verification, VerifiedIdentity) or verification.provider != 'email' or verification.provider_subject != email:
            raise InvalidIdentity('Требуется подтверждение владения именно этим email.')
        async with connect(self.path, self.timeout) as db:
            async with transaction(db):
                account = await self._new(db)
                await self._insert_identity(db, account.id, verification)
                return Account(**await _one(db, 'SELECT * FROM accounts WHERE id=?', (account.id,)))

    async def _add_verified_identity(self, key, *, verification):
        """INTERNAL ONLY. Caller must authorize target account AND verify identity ownership.

        Future linking requires its own authenticated two-sided approval flow.
        This low-level operation cannot be exposed as a public arbitrary-ID endpoint.
        """
        key = account_id(key)
        async with connect(self.path, self.timeout) as db:
            async with transaction(db):
                if not await _one(db, 'SELECT id FROM accounts WHERE id=?', (key,)):
                    raise AccountNotFound('Аккаунт не найден.')
                return await self._insert_identity(db, key, verification)

"""Explicit additive migration. Never imported by bot.py or database.init_db()."""
import hashlib

from .errors import SchemaCompatibilityError
from .storage import connect, transaction

VERSION = 1
NAME = 'accounts_and_verified_identities'
NOW = "(strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
DDL = {
    'account_schema_migrations': f'''CREATE TABLE account_schema_migrations (
        version INTEGER NOT NULL PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT {NOW}
    )''',
    'accounts': f'''CREATE TABLE accounts (
        id TEXT NOT NULL PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT {NOW},
        updated_at TEXT NOT NULL DEFAULT {NOW}
    )''',
    'account_identities': f'''CREATE TABLE account_identities (
        id TEXT NOT NULL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
        provider TEXT NOT NULL CHECK(length(provider) BETWEEN 2 AND 32),
        provider_subject TEXT NOT NULL CHECK(length(provider_subject) BETWEEN 1 AND 255),
        verified_at TEXT NOT NULL CHECK(length(verified_at)>0),
        created_at TEXT NOT NULL DEFAULT {NOW}
    )''',
    'account_identity_subject': '''CREATE UNIQUE INDEX account_identity_subject
        ON account_identities(provider,provider_subject)''',
    'account_identity_one_max': '''CREATE UNIQUE INDEX account_identity_one_max
        ON account_identities(account_id) WHERE provider='max' ''',
    'account_identity_owner': '''CREATE INDEX account_identity_owner ON account_identities(account_id)''',
}


def canonical(sql):
    return ' '.join(sql.split()).rstrip(';')


CHECKSUM = hashlib.sha256('\n'.join(canonical(sql) for sql in DDL.values()).encode()).hexdigest()


async def _objects(db):
    cursor = await db.execute('SELECT name,type,tbl_name,sql FROM sqlite_master')
    return {row['name'].lower(): dict(row) for row in await cursor.fetchall()}


async def _validate(db):
    objects = await _objects(db)
    for name, sql in DDL.items():
        obj = objects.get(name)
        if not obj or not obj['sql'] or canonical(obj['sql']) != canonical(sql):
            raise SchemaCompatibilityError('Несовместимая схема аккаунтов: '+name+'. Миграция остановлена.')
    tables = ('accounts', 'account_identities', 'account_schema_migrations')
    for obj in objects.values():
        if obj['tbl_name'] in tables and obj['type'] in ('index', 'trigger') and obj['sql'] and obj['name'] not in DDL:
            raise SchemaCompatibilityError('Обнаружены незарегистрированные ограничения/триггеры таблиц аккаунтов.')
    cursor = await db.execute('SELECT version,name,checksum FROM account_schema_migrations ORDER BY version')
    versions = [tuple(r) for r in await cursor.fetchall()]
    if versions != [(VERSION, NAME, CHECKSUM)]:
        raise SchemaCompatibilityError('Неизвестная версия или изменённая контрольная сумма миграции аккаунтов.')
    cursor = await db.execute('PRAGMA foreign_key_check(account_identities)')
    if await cursor.fetchone():
        raise SchemaCompatibilityError('Нарушена связь идентичности с аккаунтом.')


async def inspect_schema(path, *, timeout=5):
    """Read-only preflight: return pending/applied; fail closed on an unmanaged schema."""
    async with connect(path, timeout, readonly=True) as db:
        objects = await _objects(db)
        if not set(DDL).intersection(objects):
            return 'pending'
        await _validate(db)
        return 'applied'


async def migrate(path, *, timeout=5):
    """Apply to an EXISTING explicitly selected file; no journal-mode changes or legacy writes."""
    async with connect(path, timeout) as db:
        async with transaction(db):
            objects = await _objects(db)
            if set(DDL).intersection(objects):
                await _validate(db)
                return False
            for sql in DDL.values():
                await db.execute(sql)
            await db.execute('INSERT INTO account_schema_migrations(version,name,checksum) VALUES(?,?,?)',
                             (VERSION, NAME, CHECKSUM))
            await _validate(db)
            return True

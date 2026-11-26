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


def versions():
    # v1/v2 DDL and checksums stay unchanged; the runner also recognizes v3.
    from auth.schema import DDL as auth_ddl, NAME as auth_name
    auth_checksum = hashlib.sha256('\n'.join(canonical(sql) for sql in auth_ddl.values()).encode()).hexdigest()
    from auth.browser_schema import DDL as browser_ddl, NAME as browser_name
    browser_checksum = hashlib.sha256('\n'.join(canonical(sql) for sql in browser_ddl.values()).encode()).hexdigest()
    from account_linking.schema import DDL as linking_ddl, NAME as linking_name
    linking_checksum = hashlib.sha256('\n'.join(canonical(sql) for sql in linking_ddl.values()).encode()).hexdigest()
    from entitlements.schema import DDL as rights_ddl, NAME as rights_name
    rights_checksum = hashlib.sha256('\n'.join(canonical(sql) for sql in rights_ddl.values()).encode()).hexdigest()
    return {1: (NAME, CHECKSUM, DDL), 2: (auth_name, auth_checksum, auth_ddl),
            3: (browser_name, browser_checksum, browser_ddl),
            4: (linking_name, linking_checksum, linking_ddl),
            5: (rights_name, rights_checksum, rights_ddl)}


async def _validate(db):
    known = versions()
    objects = await _objects(db)
    all_names = set().union(*(set(v[2]) for v in known.values()))
    if not all_names.intersection(objects):
        return 0
    ledger = objects.get('account_schema_migrations')
    if not ledger or not ledger['sql'] or canonical(ledger['sql']) != canonical(DDL['account_schema_migrations']):
        raise SchemaCompatibilityError('Несовместимый журнал миграций аккаунтов.')
    cursor = await db.execute('SELECT version,name,checksum FROM account_schema_migrations ORDER BY version')
    applied = [tuple(r) for r in await cursor.fetchall()]
    if not applied or [r[0] for r in applied] != list(range(1,len(applied)+1)):
        raise SchemaCompatibilityError('Нарушена последовательность миграций.')
    for version, name, checksum in applied:
        if version not in known or (name,checksum) != known[version][:2]:
            raise SchemaCompatibilityError('Неизвестная версия или контрольная сумма миграции.')
    expected = {}
    for version, _, _ in applied:
        expected.update(known[version][2])
    for name, sql in expected.items():
        obj = objects.get(name)
        if not obj or not obj['sql'] or canonical(obj['sql']) != canonical(sql):
            raise SchemaCompatibilityError('Несовместимая схема: '+name)
    if (all_names-set(expected)).intersection(objects):
        raise SchemaCompatibilityError('Обнаружена частичная неприменённая миграция.')
    tables = {name for name, sql in expected.items() if sql.startswith('CREATE TABLE')}
    for obj in objects.values():
        if obj['tbl_name'] in tables and obj['type'] in ('index','trigger') and obj['sql'] and obj['name'] not in expected:
            raise SchemaCompatibilityError('Незарегистрированные ограничения/триггеры.')
    for table in tables:
        cursor = await db.execute('PRAGMA foreign_key_check('+table+')')
        if await cursor.fetchone():
            raise SchemaCompatibilityError('Нарушены внешние ключи.')
    return len(applied)


async def inspect_schema(path, *, timeout=5):
    async with connect(path, timeout, readonly=True) as db:
        current = await _validate(db)
        return 'applied' if current == max(versions()) else 'pending'


async def migrate(path, *, timeout=5, target=None):
    known = versions()
    target = max(known) if target is None else target
    if type(target) is not int or target not in known:
        raise SchemaCompatibilityError('Неизвестная целевая версия.')
    async with connect(path, timeout) as db:
        async with transaction(db):
            current = await _validate(db)
            if current > target:
                raise SchemaCompatibilityError('Понижение версии запрещено.')
            changed = current < target
            for version in range(current+1,target+1):
                name, checksum, definitions = known[version]
                for sql in definitions.values():
                    await db.execute(sql)
                await db.execute('INSERT INTO account_schema_migrations(version,name,checksum) VALUES(?,?,?)',
                                 (version,name,checksum))
                await _validate(db)
            return changed

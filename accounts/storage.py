"""Explicit, short-lived SQLite connections; no default database or import side effects."""
from contextlib import asynccontextmanager
import math
from pathlib import Path
import sqlite3

import aiosqlite

from .errors import AccountStoreBusy, AccountStorageError


def database_path(value):
    if not isinstance(value, (str, Path)) or not str(value).strip() or str(value) == ':memory:':
        raise ValueError('Укажите явный путь к существующей SQLite-базе.')
    return Path(value).expanduser().resolve()


def connection_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 30:
        raise ValueError('Таймаут SQLite должен быть больше 0 и не больше 30 секунд.')
    return float(value)


@asynccontextmanager
async def connect(path, timeout, *, readonly=False):
    uri = database_path(path).as_uri() + ('?mode=ro' if readonly else '?mode=rw')
    try:
        async with aiosqlite.connect(uri, uri=True, timeout=connection_timeout(timeout)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute('PRAGMA foreign_keys=ON')
            yield db
    except sqlite3.OperationalError as exc:
        if (getattr(exc, 'sqlite_errorcode', 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise AccountStoreBusy('SQLite занята. Операция не завершена; повторите её позднее.') from None
        raise AccountStorageError('Не удалось выполнить операцию аккаунтов. Проверьте путь и применение миграции.') from None
    except sqlite3.DatabaseError:
        raise AccountStorageError('Операция аккаунтов отклонена базой данных.') from None


@asynccontextmanager
async def transaction(db):
    try:
        await db.execute('BEGIN IMMEDIATE')
        yield
        await db.commit()
    except BaseException:
        await db.rollback()
        raise

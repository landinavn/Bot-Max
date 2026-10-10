import pytest

import database


@pytest.fixture
async def credit_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "users.sqlite3")
    await database.init_db()
    return database

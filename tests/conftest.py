import pytest

import database


@pytest.fixture
async def credit_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "users.sqlite3")
    await database.init_db()
    return database


@pytest.fixture(autouse=True)
def mock_music_composer_and_preview(monkeypatch):
    # HTTP тесты audio_service вызывают реальные функции с respx; интеграционные
    # тесты очереди/роутера не обращаются к платным моделям или Object Storage.
    from unittest.mock import AsyncMock
    import handlers
    import audio_orders
    writer = AsyncMock(side_effect=lambda notes, tags, title: notes)
    monkeypatch.setattr(handlers, 'write_song_lyrics', writer)
    monkeypatch.setattr(audio_orders, 'write_song_lyrics', writer)
    monkeypatch.setattr(audio_orders, 'make_protected_preview', AsyncMock(return_value='https://preview.example/clip.mp3'))

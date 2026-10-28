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
    writer = AsyncMock(side_effect=lambda notes, tags, title, **kwargs: notes)
    monkeypatch.setattr(handlers, 'write_song_lyrics', writer)
    monkeypatch.setattr(handlers, 'generate_song', AsyncMock(return_value='inline-test-job'))
    result = {'status': 'SUCCESS', 'data': {'musics': [{'audioUrl': 'https://audio.example/1.mp3', 'title': 'Один'}, {'audioUrl': 'https://audio.example/2.mp3', 'title': 'Два'}]}}
    monkeypatch.setattr(handlers, 'wait_for_song', AsyncMock(return_value=result))
    monkeypatch.setattr(handlers, 'wait_for_song_result', AsyncMock(return_value=result))
    monkeypatch.setattr(handlers, 'make_protected_preview', AsyncMock(return_value='https://preview.example/clip.mp3'))
    monkeypatch.setattr(handlers, 'merge_video_and_audio', AsyncMock(side_effect=lambda video, audio: video))
    monkeypatch.setattr(audio_orders, 'write_song_lyrics', writer)
    monkeypatch.setattr(audio_orders, 'make_protected_preview', AsyncMock(return_value='https://preview.example/clip.mp3'))


@pytest.fixture(autouse=True)
def mock_metrika_upload(monkeypatch):
    # Сервис тестируется отдельно; роутер не отправляет аналитику в настоящий счётчик.
    from unittest.mock import AsyncMock
    import handlers
    monkeypatch.setattr(handlers, 'send_offline_conversion', AsyncMock())


@pytest.fixture(autouse=True)
def mock_video_people_counter(monkeypatch):
    from unittest.mock import AsyncMock
    import handlers
    import video_service
    monkeypatch.setattr(handlers, 'count_people_in_photo', AsyncMock(return_value=1))
    monkeypatch.setattr(video_service, 'count_people_in_photo', AsyncMock(return_value=1))

import csv
import io
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aioresponses import aioresponses

import metrika_service as service
from config import Config

URL = service.BASE_URL + '/management/v1/counter/123456/offline_conversions/upload'


@pytest.fixture
def metrika_config(monkeypatch):
    monkeypatch.setattr(service.Config, 'from_env', lambda: Config(token='max-test', photo_path=Path('unused'),
        yandex_metrika_counter_id='123456', yandex_metrika_oauth_token='test-oauth-secret'))
    monkeypatch.setattr(service.time, 'time', lambda: 1800000000.9)


@pytest.mark.parametrize('payload,expected', [
    ('yclid=12345678901234567890', '12345678901234567890'),
    ('yandex_newyear?yclid=12345', '12345'),
    ('yandex_newyear&yclid=12345&foo=bar', '12345'),
    ('/start yandex_newyear?yclid=12345', '12345'),
    ('https://max.ru/test?start=yandex_newyear&yclid=12345', '12345'),
    ('yandex_newyear%3Fyclid%3D12345', '12345'),
    ('yandex_newyear%253Fyclid%253D12345', '12345'),
    ('{"yclid":"12345"}', '12345'),
    ({'yclid': 12345}, '12345'),
    ({'payload': 'yclid=12345'}, '12345'),
    (None, None), ('', None), ('yandex_newyear', None), ('share_token', None),
    ('notyclid=12345', None), ('yclid=', None), ('yclid=' + '1'*257, None),
    ({'yclid': True}, None), ('{invalid json', None),
])
def test_extract_yclid(payload, expected):
    assert service.extract_yclid(payload) == expected


@pytest.mark.parametrize('yclid', [None, '', '12345678901234567890'])
async def test_csv_multipart_authorization_and_past_timestamp(credit_db, metrika_config, yclid):
    if yclid is not None:
        await credit_db.save_yclid(456, yclid)
    with aioresponses() as http:
        http.post(URL, payload={'uploading': {'id': 1, 'status': 'UPLOADED'}})
        await service.send_offline_conversion(456)
    request = next(iter(http.requests.values()))[0].kwargs
    assert request['headers'] == {'Authorization': 'OAuth test-oauth-secret'}
    assert request['allow_redirects'] is False
    form = request['data']
    assert form.is_multipart
    options, headers, contents = form._fields[0]
    assert options['name'] == 'file' and options['filename'] == 'conversions.csv'
    assert headers['Content-Type'] == 'text/csv; charset=utf-8'
    rows = list(csv.reader(io.StringIO(contents.decode('utf-8'))))
    assert rows[0] == ['Target', 'DateTime', 'UserId'] + (['Yclid'] if yclid else [])
    assert rows[1] == ['bot_generation', '1799999999', '456'] + ([yclid] if yclid else [])


@pytest.mark.parametrize('counter,token', [(None, None), ('123456', None), (None, 'token')])
async def test_missing_settings_make_no_request(monkeypatch, caplog, counter, token):
    monkeypatch.setattr(service.Config, 'from_env', lambda: Config(token='max', photo_path=Path('unused'),
        yandex_metrika_counter_id=counter, yandex_metrika_oauth_token=token))
    with aioresponses() as http:
        await service.send_offline_conversion(456)
    assert not http.requests
    assert 'не заданы YANDEX_METRIKA_COUNTER_ID' in caplog.text


@pytest.mark.parametrize('status', [400, 401, 429, 500])
async def test_api_error_logged_without_raising_or_leaking_token(credit_db, metrika_config, caplog, status):
    with aioresponses() as http:
        http.post(URL, status=status, body='invalid upload test-oauth-secret')
        await service.send_offline_conversion(456)
    assert f'Ошибка Метрики: HTTP {status}' in caplog.text
    assert 'invalid upload' in caplog.text
    assert 'test-oauth-secret' not in caplog.text
    assert len(next(iter(http.requests.values()))) == 1


@pytest.mark.parametrize('error', [TimeoutError(), aiohttp.ClientConnectionError('connection failed')])
async def test_timeout_or_network_failure_is_nonfatal(credit_db, metrika_config, caplog, error):
    with aioresponses() as http:
        http.post(URL, exception=error)
        await service.send_offline_conversion(456)
    assert 'Ошибка Метрики:' in caplog.text


async def test_database_failure_is_nonfatal(metrika_config, monkeypatch, caplog):
    monkeypatch.setattr(service, 'get_user', AsyncMock(side_effect=RuntimeError('database unavailable')))
    with aioresponses() as http:
        await service.send_offline_conversion(456)
    assert not http.requests
    assert 'Ошибка Метрики:' in caplog.text

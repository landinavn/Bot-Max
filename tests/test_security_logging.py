"""Synthetic-only privacy regressions for application, SDK and recovery logs."""
import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from safe_logging import get_logger, correlation_scope
from max_client import log_max_error
from multstudio.diagnostics import log_preflight_failure

PRIVATE = [
    'СинтетическаяВзрослаяМария', 'СинтетическийРебёнокЛёва',
    'Синтетическое поздравление о семейной истории', 'audit-person@example.invalid',
    '+7-000-123-45-67', 'SYNTHETIC_SECRET_TOKEN_93217',
    'https://private.example.invalid/photo.png?signature=SYNTHETIC_SIGNATURE',
]


def assert_private_absent(caplog):
    for value in PRIVATE:
        assert value not in caplog.text
    for record in caplog.records:
        assert record.args == ()
        assert record.exc_info is None
        assert record.exc_text is None
        assert record.stack_info is None
        payload = json.loads(record.getMessage())
        assert set(payload) <= {'event','source','stage','line','product','http_status',
                                'duration_ms','correlation','error_code'}


@pytest.mark.parametrize('payload', PRIVATE + [{'child': PRIVATE[1], 'nested': {'Authorization': PRIVATE[5], 'url': PRIVATE[6]}}])
@pytest.mark.parametrize('level', ['info', 'warning', 'error', 'exception'])
def test_legacy_application_messages_and_objects_never_reach_handlers(caplog, payload, level):
    with caplog.at_level(logging.INFO):
        getattr(get_logger('handlers'), level)(payload, extra={'Authorization': PRIVATE[5]}, exc_info=True, stack_info=True)
    assert caplog.records
    assert_private_absent(caplog)


@pytest.mark.parametrize('name',['httpx','httpcore','aiohttp.client','bot','connection','dispatcher','maxapi', 'openai', 'botocore', PRIVATE[3]])
def test_sdk_untrusted_messages_nested_json_and_chained_tracebacks_are_dropped(caplog,name):
    with caplog.at_level(logging.INFO):
        try:
            try:
                raise ValueError(' '.join(PRIVATE))
            except ValueError as cause:
                raise RuntimeError(json.dumps({'private': PRIVATE})) from cause
        except RuntimeError:
            logging.getLogger(name).exception({'nested': PRIVATE}, stack_info=True)
            logging.getLogger(name).info('request %s headers %s',PRIVATE[6],{'Authorization': PRIVATE[5]})
    assert len(caplog.records)==2
    assert_private_absent(caplog)


def test_logging_never_calls_sensitive_object_str_or_repr(caplog):
    class Unprintable:
        def __str__(self): raise AssertionError('Must never format raw object')
        def __repr__(self): raise AssertionError('Must never format raw object')
    with caplog.at_level(logging.ERROR):
        get_logger('handlers').error('%s',Unprintable(),exc_info=(ValueError,Unprintable(),None))
        logging.getLogger('httpx').error(Unprintable())
    assert_private_absent(caplog)


async def test_max_error_does_not_read_body_headers_or_exception_text(caplog):
    class PrivateError(Exception):
        status_code=403
        def __str__(self): raise AssertionError('Sensitive exception text accessed')
        @property
        def raw(self): raise AssertionError('Sensitive raw body accessed')
        @property
        def response(self): raise AssertionError('Sensitive response accessed')
    await log_max_error(PrivateError(),logging.getLogger('max_client'))
    assert_private_absent(caplog)
    payload=json.loads(caplog.records[-1].getMessage())
    assert payload['event']=='request_failed' and payload['http_status']==403


def test_recovery_preflight_keeps_diagnostics_without_job_secrets_or_traceback(caplog):
    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError(' '.join(PRIVATE))
        except ValueError as exc:
            log_preflight_failure(logging.getLogger('multstudio.worker'), PRIVATE[6],exc,
                                  SimpleNamespace(heygen_key=PRIVATE[5]),SimpleNamespace(config=PRIVATE))
    assert_private_absent(caplog)
    assert json.loads(caplog.records[-1].getMessage())['event']=='preflight_failed'


def test_explicit_events_have_only_allowed_fields_and_random_operation_correlation(caplog):
    with caplog.at_level(logging.INFO), correlation_scope():
        logger=get_logger('image_service')
        logger.event('started',product='image',stage='generate',http_status=200)
        logger.event('completed',product='image',duration_ms=17)
    rows=[json.loads(r.getMessage()) for r in caplog.records]
    assert rows[0]['correlation']==rows[1]['correlation']
    assert len(rows[0]['correlation'])==16
    assert rows[0]['http_status']==200 and rows[1]['duration_ms']==17
    assert_private_absent(caplog)


def test_invalid_optional_fields_cannot_forward_personal_values(caplog):
    with caplog.at_level(logging.ERROR):
        get_logger('handlers').event(PRIVATE[0],level=logging.ERROR,product=PRIVATE[1],
                                    stage=PRIVATE[2],http_status=PRIVATE[3],duration_ms=PRIVATE[4])
    assert_private_absent(caplog)
    assert json.loads(caplog.records[-1].getMessage())['event']=='diagnostic'


def test_all_runtime_loggers_use_safe_facade_and_explicit_events_are_static():
    root=Path(__file__).resolve().parents[1]
    for path in list(root.glob('*.py'))+list((root/'multstudio').glob('*.py')):
        if path.name=='safe_logging.py':continue
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
                assert not (isinstance(node.func.value,ast.Name) and node.func.value.id=='logging' and node.func.attr=='getLogger'),path
                if node.func.attr=='event':
                    assert node.args and isinstance(node.args[0],ast.Constant),path


@pytest.mark.parametrize('provider',['heygen','speechkit','kling','ttapi'])
async def test_error_api_bodies_and_recovery_exception_never_leak_or_repeat_post(monkeypatch,caplog,provider):
    import httpx
    import respx
    from aioresponses import aioresponses
    from heygen import HeyGenAdapter,HeyGenError
    from multstudio.adapters import YandexTTSAdapter,ProviderFailed
    import audio_service
    import video_service
    private_body=json.dumps({'child':PRIVATE[1],'text':PRIVATE[2], 'nested':{'email':PRIVATE[3],'auth':PRIVATE[5],'url':PRIVATE[6]}})
    calls=[]
    def fail(request):calls.append(request);return httpx.Response(400,text=private_body)
    with caplog.at_level(logging.INFO):
        if provider=='heygen':
            with pytest.raises(HeyGenError):
                await HeyGenAdapter(PRIVATE[5],httpx.MockTransport(fail)).request('POST','/v3/models/audio/tts',payload={'text':PRIVATE[2]})
        elif provider=='speechkit':
            with pytest.raises(ProviderFailed):
                await YandexTTSAdapter(PRIVATE[5],transport=httpx.MockTransport(fail)).synthesize(PRIVATE[2],'alena')
        elif provider=='kling':
            monkeypatch.setenv('PIAPI_KEY',PRIVATE[5]);monkeypatch.setenv('PIAPI_API_KEY',PRIVATE[5])
            with respx.mock() as api:
                route=api.post(video_service.TASK_URL).mock(side_effect=fail)
                with pytest.raises(video_service.VideoServiceError):
                    await video_service.create_video_task(PRIVATE[6],PRIVATE[2])
                assert route.call_count==1
        else:
            monkeypatch.setenv('TTAPI_KEY',PRIVATE[5])
            with aioresponses() as api:
                api.post(audio_service.BASE_URL+'/suno/v1/music',status=400,body=private_body)
                with pytest.raises(audio_service.AudioServiceError) as caught:
                    await audio_service.generate_song(PRIVATE[2],'pop','synthetic')
                assert sum(len(v) for v in api.requests.values())==1
                # Simulate the recovery worker's chained/error log path.
                get_logger('audio_orders').exception(caught.value,exc_info=True)
    if provider!='ttapi':assert len(calls)==1
    assert_private_absent(caplog)


def test_extra_headers_are_not_kept_on_sdk_records_or_rendered(caplog):
    with caplog.at_level(logging.ERROR):
        logging.getLogger('httpx').error('unsafe',extra={'Authorization':PRIVATE[5],'headers':{'email':PRIVATE[3]},'payload':PRIVATE})
    assert_private_absent(caplog)
    assert all('Authorization' not in r.__dict__ and 'headers' not in r.__dict__ and 'payload' not in r.__dict__ for r in caplog.records)

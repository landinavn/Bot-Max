import asyncio
from dataclasses import replace
import smtplib
import ssl
import threading
from unittest.mock import MagicMock

import pytest

from auth.email_sender import LoginEmail
from auth.errors import DeliveryFailed
from auth.smtp import DeliveryUncertain, SMTPEmailSender, SMTPSettings


@pytest.fixture
def smtp(monkeypatch):
    connection=MagicMock();connection.send_message.return_value={}
    plain=MagicMock(return_value=connection);tls=MagicMock(return_value=connection)
    monkeypatch.setattr(smtplib,'SMTP',plain);monkeypatch.setattr(smtplib,'SMTP_SSL',tls)
    config=SMTPSettings('smtp.example.test',587,'noreply@example.test',username='synthetic',password='test-only')
    message=LoginEmail('a@example.test','Код для входа','Код: 123456','test')
    return config,message,connection,plain,tls


@pytest.mark.parametrize('mode',['starttls','tls'])
async def test_tls_certificate_checks_utf8_and_no_fallback(smtp,mode):
    config,message,connection,plain,tls=smtp
    await SMTPEmailSender(replace(config,tls_mode=mode)).send(message)
    constructor=tls if mode=='tls' else plain
    context=constructor.call_args.kwargs['context'] if mode=='tls' else connection.starttls.call_args.kwargs['context']
    assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
    assert constructor.call_args.kwargs['timeout']==5
    assert connection.send_message.call_count==1
    mail=connection.send_message.call_args.args[0]
    assert mail['Subject']=='Код для входа' and mail.get_content_charset()=='utf-8'
    assert mail.get_content().strip()=='Код: 123456'
    if mode=='tls':plain.assert_not_called();connection.starttls.assert_not_called()
    else:tls.assert_not_called()


@pytest.mark.parametrize('failure',[smtplib.SMTPNotSupportedError('TLS unavailable'),ssl.SSLCertVerificationError('bad'),smtplib.SMTPAuthenticationError(535,b'bad'),TimeoutError()])
async def test_before_submission_is_proven_failure(smtp,failure):
    config,message,connection,_,_=smtp;connection.starttls.side_effect=failure
    with pytest.raises(DeliveryFailed):await SMTPEmailSender(config).send(message)
    connection.send_message.assert_not_called()


@pytest.mark.parametrize('failure',[smtplib.SMTPRecipientsRefused({'a@example.test':(550,b'no')}),smtplib.SMTPDataError(554,b'rejected'),smtplib.SMTPSenderRefused(550,b'no','noreply@example.test')])
async def test_explicit_submission_rejections_are_definite(smtp,failure):
    config,message,connection,_,_=smtp;connection.send_message.side_effect=failure
    with pytest.raises(DeliveryFailed):await SMTPEmailSender(config).send(message)
    assert connection.send_message.call_count==1


@pytest.mark.parametrize('failure',[TimeoutError(),smtplib.SMTPServerDisconnected(),OSError('secret server detail')])
async def test_uncertain_submission_never_retries_or_leaks(smtp,failure,caplog):
    config,message,connection,_,_=smtp;connection.send_message.side_effect=failure
    with pytest.raises(DeliveryUncertain) as error:await SMTPEmailSender(config).send(message)
    assert str(error.value)=='' and '123456' not in caplog.text
    assert connection.send_message.call_count==1


async def test_cancellation_keeps_thread_slot_and_does_not_retry(smtp):
    config,message,connection,_,_=smtp;start=threading.Event();release=threading.Event()
    def sending(*a,**k):start.set();release.wait(2);return {}
    connection.send_message.side_effect=sending
    sender=SMTPEmailSender(config)
    task=asyncio.create_task(sender.send(message))
    try:
        for _ in range(100):
            if start.is_set():break
            await asyncio.sleep(.005)
        assert start.is_set();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert sender._slots.acquire(blocking=False)
        assert not sender._slots.acquire(blocking=False)
        sender._slots.release()
    finally:
        release.set()
        for _ in range(100):
            if sender._slots._value==2:break
            await asyncio.sleep(.005)
    assert connection.send_message.call_count==1


@pytest.mark.parametrize('field,value',[('tls_mode','none'),('from_name','A\r\nBcc:x'),('from_email','a@example.test\n'),('port',True),('timeout',float('inf')),('host','smtp://user:pass@example.test')])
def test_unsafe_configuration_rejected(smtp,field,value):
    with pytest.raises(ValueError):replace(smtp[0],**{field:value})


async def test_header_injection_rejected_before_network(smtp):
    config,message,_,plain,tls=smtp
    with pytest.raises(DeliveryFailed):await SMTPEmailSender(config).send(replace(message,subject='hello\r\nBcc: victim@example.test'))
    plain.assert_not_called();tls.assert_not_called()
    assert 'test-only' not in repr(config)

"""Actual local TLS handshakes + real maxapi transport. No MAX/provider calls."""
import asyncio
import json
import ssl
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import aiohttp
from aiohttp import web
import pytest
from maxapi.enums import UploadType
from maxapi.enums.http_method import HTTPMethod
from maxapi.types import InputMediaBuffer

import max_client


@pytest.fixture(scope='module')
def certificates(tmp_path_factory):
    root=tmp_path_factory.mktemp('synthetic-local-ca')
    def run(*args):
        subprocess.run(['openssl',*map(str,args)],cwd=root,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    run('req','-x509','-newkey','rsa:2048','-nodes','-keyout','ca.key','-out','ca.pem','-days','2','-subj','/CN=SyntheticSecurityTestCA')
    (root/'index').write_text('');(root/'serial').write_text('1000\n');(root/'newcerts').mkdir()
    for name,san in [('valid','IP:127.0.0.1,DNS:localhost'),('expired','IP:127.0.0.1'),('hostname','DNS:unrelated.invalid')]:
        run('req','-new','-newkey','rsa:2048','-nodes','-keyout',name+'.key','-out',name+'.csr','-subj','/CN=synthetic-local-test')
        config=f'''[ ca ]
default_ca=local
[ local ]
database=index
serial=serial
new_certs_dir=newcerts
certificate=ca.pem
private_key=ca.key
default_md=sha256
policy=policy
unique_subject=no
[ policy ]
commonName=supplied
[ leaf ]
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName={san}
'''
        (root/'ca.cnf').write_text(config)
        dates=['-startdate','20000101000000Z','-enddate','20010101000000Z'] if name=='expired' else ['-days','2']
        run('ca','-batch','-notext','-config','ca.cnf','-extensions','leaf','-in',name+'.csr','-out',name+'.pem',*dates)
    return root


async def start_https(certificates, certificate, handler):
    app=web.Application();app.router.add_route('*','/{path:.*}',handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates/(certificate+'.pem'),certificates/(certificate+'.key'))
    runner=web.AppRunner(app,access_log=None);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0,ssl_context=context);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    return runner,f'https://127.0.0.1:{port}'


async def test_connector_uses_system_context_and_never_sdk_extra_ca(monkeypatch):
    original=ssl.create_default_context
    called=[]
    def factory():
        called.append(True);return original()
    monkeypatch.setattr(max_client.ssl,'create_default_context',factory)
    connector=max_client.MaxAPIConnector()
    try:
        assert called==[True]
        assert isinstance(connector._ssl,ssl.SSLContext)
        assert connector._ssl.verify_mode==ssl.CERT_REQUIRED
        assert connector._ssl.check_hostname is True
        assert connector._ssl.cert_store_stats()['x509_ca']>0
    finally:await connector.close()


@pytest.mark.parametrize('certificate,trust_local_ca,accepted',[
    ('valid',True,True),('valid',False,False),('expired',True,False),('hostname',True,False)])
async def test_real_tls_valid_unknown_expired_and_wrong_hostname(certificates,monkeypatch,certificate,trust_local_ca,accepted):
    received=[]
    async def handler(request):
        received.append(request.path);return web.json_response({'ok':True})
    runner,url=await start_https(certificates,certificate,handler)
    original=ssl.create_default_context
    context=original(cafile=str(certificates/'ca.pem')) if trust_local_ca else original()
    monkeypatch.setattr(max_client.ssl,'create_default_context',lambda:context)
    connector=max_client.MaxAPIConnector()
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            if accepted:
                async with session.get(url) as response:assert await response.json()=={'ok':True}
            else:
                with pytest.raises(aiohttp.ClientConnectorCertificateError):await session.get(url)
        assert received==(['/'] if accepted else [])
        assert connector.closed
    finally:
        await connector.close();await runner.cleanup()


async def test_real_sdk_poll_messages_callbacks_send_edit_upload_download_and_results(certificates,monkeypatch):
    seen=[];base=None
    message={'recipient':{'chat_id':123,'chat_type':'dialog'},'timestamp':1,
             'body':{'mid':'synthetic-mid','seq':1,'text':'synthetic greeting'}}
    async def handler(request):
        seen.append((request.method,request.path))
        if request.path=='/updates':
            return web.json_response({'marker':42,'updates':[{'update_type':'message_created','message':message},
                {'update_type':'message_callback','callback':{'callback_id':'synthetic-callback'}}]})
        if request.path=='/messages' and request.method=='POST':return web.json_response({'message':message})
        if request.path=='/messages':return web.json_response({'success':True})
        if request.path=='/uploads':return web.json_response({'url':base+'/upload','token':'synthetic-upload-token'})
        if request.path=='/upload':
            await request.read();return web.json_response({'token':'synthetic-file-token','photos':{'1':{'token':'synthetic-image-token'}}})
        if request.path=='/attachment':return web.Response(body=b'synthetic attachment',content_type='application/octet-stream')
        return web.json_response({'success':True})
    runner,base=await start_https(certificates,'valid',handler)
    context=ssl.create_default_context(cafile=str(certificates/'ca.pem'))
    monkeypatch.setattr(max_client.ssl,'create_default_context',lambda:context)
    monkeypatch.setattr(max_client,'MAX_API_URL',base)
    try:
        async with max_client.create_max_bot('synthetic-token',auto_requests=False) as bot:
            updates=await bot.get_updates(timeout=1)
            assert {u['update_type'] for u in updates['updates']}=={'message_created','message_callback'}
            await bot.send_message(user_id=123,text='synthetic greeting')
            await bot.edit_message('synthetic-mid',text='synthetic revision')
            await bot.send_callback(callback_id='synthetic-callback',notification='synthetic acknowledgement')
            for kind in (UploadType.IMAGE,UploadType.FILE,UploadType.AUDIO,UploadType.VIDEO):
                uploaded=await bot.upload_media(InputMediaBuffer(buffer=b'synthetic-media-bytes',filename='synthetic',type=kind))
                assert uploaded.payload.token
                await bot.send_message(user_id=123,attachments=[uploaded])
            assert await bot.download_bytes(base+'/attachment')==b'synthetic attachment'
            session=bot.session;connector=bot.default_connection.kwargs['connector']
            assert session is not None and not session.closed
        assert session.closed and connector.closed
        assert seen.count(('POST','/upload'))==4 and ('GET','/updates') in seen
    finally:await runner.cleanup()


async def test_session_connector_close_on_failed_setup_and_cancel(monkeypatch):
    bot=Mock();bot.set_api_url.side_effect=ValueError('synthetic setup failure');bot.close_session=AsyncMock()
    factory=Mock(return_value=bot);monkeypatch.setattr(max_client,'Bot',factory)
    with pytest.raises(ValueError):
        async with max_client.create_max_bot('synthetic'):pass
    assert factory.call_args.kwargs['default_connection'].kwargs['connector'].closed
    bot.close_session.assert_awaited_once()
    bot.reset_mock();bot.set_api_url.side_effect=None
    with pytest.raises(asyncio.CancelledError):
        async with max_client.create_max_bot('synthetic'):raise asyncio.CancelledError()
    bot.close_session.assert_awaited_once()
    assert factory.call_args.kwargs['default_connection'].kwargs['connector'].closed


@pytest.mark.parametrize('certificate,trust_local_ca',[('valid',False),('expired',True),('hostname',True)])
@pytest.mark.parametrize('operation',['poll','upload','download'])
async def test_sdk_tls_failure_rejects_api_upload_and_download_without_fallback(certificates,monkeypatch,certificate,trust_local_ca,operation):
    from maxapi.exceptions.max import MaxConnection
    from maxapi.exceptions.download_file import DownloadFileError
    received=[]
    async def handler(request):
        received.append(request.path);return web.json_response({'success':True})
    runner,url=await start_https(certificates,certificate,handler)
    context=ssl.create_default_context(cafile=str(certificates/'ca.pem')) if trust_local_ca else ssl.create_default_context()
    monkeypatch.setattr(max_client.ssl,'create_default_context',lambda:context)
    monkeypatch.setattr(max_client,'MAX_API_URL',url)
    try:
        async with max_client.create_max_bot('synthetic-token',auto_requests=False) as bot:
            # Avoid SDK backoff delays in the local test. No production retry changes.
            bot.default_connection.max_retries=0
            if operation=='poll':
                with pytest.raises(MaxConnection):await bot.get_updates(timeout=1)
            elif operation=='upload':
                with pytest.raises(aiohttp.ClientConnectorCertificateError):
                    await bot.upload_file_buffer(filename='synthetic',url=url+'/upload',buffer=b'synthetic',type=UploadType.FILE)
            else:
                with pytest.raises((DownloadFileError,aiohttp.ClientConnectorCertificateError)):
                    await bot.download_bytes(url+'/attachment')
            connector=bot.default_connection.kwargs['connector']
        assert connector.closed and received==[]
    finally:await runner.cleanup()


async def test_max_connector_rejects_plain_http_before_any_connection():
    async with max_client.MaxAPIConnector() as connector:
        async with aiohttp.ClientSession(connector=connector) as session:
            with pytest.raises(aiohttp.ClientConnectionError,match='requires HTTPS'):
                await session.get('http://127.0.0.1:1/plain')


async def test_https_redirect_to_http_never_reaches_plain_server(certificates,monkeypatch):
    received=[]
    async def plain(request):
        received.append(True)
        return web.json_response({'unsafe':True})
    app=web.Application();app.router.add_get('/',plain)
    plain_runner=web.AppRunner(app,access_log=None);await plain_runner.setup()
    site=web.TCPSite(plain_runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    async def redirect(request):
        raise web.HTTPFound(f'http://127.0.0.1:{port}/')
    runner,url=await start_https(certificates,'valid',redirect)
    context=ssl.create_default_context(cafile=str(certificates/'ca.pem'))
    monkeypatch.setattr(max_client.ssl,'create_default_context',lambda:context)
    try:
        async with max_client.MaxAPIConnector() as connector:
            async with aiohttp.ClientSession(connector=connector) as session:
                with pytest.raises(aiohttp.ClientConnectionError,match='requires HTTPS'):
                    await session.get(url)
        assert received==[]
    finally:
        await runner.cleanup();await plain_runner.cleanup()

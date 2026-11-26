"""HTTP-only synthetic users; temporary SQLite, fake mail, ASGI in-process transport."""
import asyncio
from dataclasses import replace
import hashlib
import json
import re
import sqlite3
import subprocess
import sys

import httpx
import pytest

from accounts.migrations import CHECKSUM, DDL, inspect_schema, migrate
from auth.email_sender import FakeEmailSender
from auth.errors import InvalidCode
from auth.service import AuthService
from auth.settings import AuthSettings
from web_api.app import create_app
from web_api.config import APISettings
from web_api.security import browser_context, client_source
from starlette.requests import Request

ORIGIN='https://app.wish-bot.ru'


class Clock:
    now=1700000000
    def __call__(self):return self.now


@pytest.fixture
async def env(tmp_path):
    db=tmp_path/'synthetic.sqlite3';db.touch();await migrate(db)
    settings=APISettings(database=db,secret=b'test-api-secret-not-for-production-123',environment='test',database_timeout=.05)
    sender=FakeEmailSender();clock=Clock()
    app=create_app(settings,sender=sender,clock=clock)
    return settings,sender,clock,app


def client(env, peer='203.0.113.42'):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=env[3],client=(peer,1234),raise_app_exceptions=False),base_url=ORIGIN)


async def bootstrap(c):
    r=await c.get('/api/v1/auth/csrf');assert r.status_code==200,r.text
    return {'Origin':ORIGIN,'X-CSRF-Token':r.json()['csrf_token']}


def code(sender):return re.search(r'Ваш код для входа: ([0-9]+)',sender.messages[-1].body)[1]


async def login(c,env,email='alice@example.test'):
    headers=await bootstrap(c)
    request=await c.post('/api/v1/auth/email/request',headers=headers,json={'email':email})
    assert request.status_code==200,request.text
    result=await c.post('/api/v1/auth/email/verify',headers=headers,json={'challenge_id':request.json()['challenge_id'],'code':code(env[1])})
    assert result.status_code==200,result.text
    return result,request,headers


async def test_registration_cookie_me_logout_and_repeat(env):
    async with client(env) as c:
        response,_,_=await login(c,env)
        account=response.json()['account']
        token=c.cookies.get(env[0].session_cookie)
        assert token not in response.text and set(response.json())=={'account','csrf_token'}
        cookies=response.headers.get_list('set-cookie'); session=next(x for x in cookies if x.startswith('__Host-wish_session='))
        assert all(x in session for x in ('HttpOnly','Secure','SameSite=lax','Path=/','Max-Age=2592000'))
        assert all('Domain=' not in h for h in cookies)
        assert any('__Host-wish_preauth=""' in h and 'Max-Age=0' in h for h in cookies)
        assert (await c.get('/api/v1/me')).json()==account
        headers={'Origin':ORIGIN,'X-CSRF-Token':response.json()['csrf_token']}
        out=await c.post('/api/v1/auth/logout',headers=headers,json={})
        assert out.status_code==200
        assert 'Max-Age=0' in out.headers['set-cookie']
        assert (await c.get('/api/v1/me')).status_code==401
        env[2].now+=60
        response2,_,_=await login(c,env)
        assert response2.json()['account']==account and c.cookies.get(env[0].session_cookie)!=token
    with sqlite3.connect(env[0].database) as d:
        assert d.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]==1
        assert d.execute('SELECT COUNT(*) FROM account_identities').fetchone()[0]==1
        assert not d.execute("SELECT name FROM sqlite_master WHERE name='users'").fetchone()


async def test_logout_all_revokes_other_browser(env):
    async with client(env) as a,client(env) as b:
        first,_,_=await login(a,env); env[2].now+=60
        await login(b,env)
        assert (await a.post('/api/v1/auth/logout-all',json={},headers={'Origin':ORIGIN,'X-CSRF-Token':first.json()['csrf_token']})).status_code==200
        assert (await b.get('/api/v1/me')).status_code==401


@pytest.mark.parametrize('cause',['expired','idle','revoked'])
async def test_invalid_sessions(env,cause):
    async with client(env) as c:
        await login(c,env)
        if cause=='expired':env[2].now+=30*86400
        elif cause=='idle':env[2].now+=7*86400
        else:
            with sqlite3.connect(env[0].database) as d:d.execute('UPDATE auth_sessions SET revoked_at=?',(env[2].now,))
        assert (await c.get('/api/v1/me')).status_code==401


@pytest.mark.parametrize('origin',[None,'null','https://smalltalk.wish-bot.ru','https://wish-bot.ru','https://evil.test','https://app.wish-bot.ru.evil.test'])
async def test_origin_denied(env,origin):
    async with client(env) as c:
        headers=await bootstrap(c)
        if origin is None:headers.pop('Origin')
        else:headers['Origin']=origin
        r=await c.post('/api/v1/auth/email/request',json={'email':'a@example.test'},headers=headers)
        assert r.status_code==403 and not env[1].messages
        assert r.headers['cache-control']=='no-store'


@pytest.mark.parametrize('token',[None,'bad','a'*64,'é'*64])
async def test_csrf_denied(env,token):
    async with client(env) as c:
        headers=await bootstrap(c)
        if token is None:headers.pop('X-CSRF-Token')
        else:headers['X-CSRF-Token']=token.encode('utf8')
        assert (await c.post('/api/v1/auth/email/request',json={'email':'a@example.test'},headers=headers)).status_code==403
        assert not env[1].messages


async def test_cross_browser_challenge_substitution_and_missing_preauth(env):
    async with client(env) as a,client(env) as b:
        ah=await bootstrap(a);bh=await bootstrap(b)
        req=await a.post('/api/v1/auth/email/request',json={'email':'a@example.test'},headers=ah)
        body={'challenge_id':req.json()['challenge_id'],'code':code(env[1])}
        assert (await b.post('/api/v1/auth/email/verify',json=body,headers=bh)).status_code==400
        b.cookies.clear()
        assert (await b.post('/api/v1/auth/email/verify',json=body,headers=ah)).status_code==403
        assert (await a.post('/api/v1/auth/email/verify',json=body,headers=ah)).status_code==200


async def test_replay_old_preauth_and_no_silent_account_switch(env):
    async with client(env) as c:
        h=await bootstrap(c); old=c.cookies.get(env[0].preauth_cookie)
        r=await c.post('/api/v1/auth/email/request',json={'email':'a@example.test'},headers=h)
        body={'challenge_id':r.json()['challenge_id'],'code':code(env[1])}
        result=await c.post('/api/v1/auth/email/verify',json=body,headers=h)
        session=c.cookies.get(env[0].session_cookie)
        c.cookies.set(env[0].preauth_cookie,old,domain='app.wish-bot.ru',path='/')
        assert (await c.post('/api/v1/auth/email/verify',json=body,headers=h)).status_code==409
        assert c.cookies.get(env[0].session_cookie)==session
        c.cookies.clear();c.cookies.set(env[0].preauth_cookie,old,domain='app.wish-bot.ru',path='/')
        assert (await c.post('/api/v1/auth/email/verify',json=body,headers=h)).status_code==400
        assert (await c.post('/api/v1/auth/email/request',json={'email':'b@example.test'},headers=h)).status_code==400
        assert result.status_code==200


async def test_resend_new_email_multiple_tabs_and_context_expiry(env):
    async with client(env) as c:
        h=await bootstrap(c);assert (await bootstrap(c))==h
        one=await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'});oldcode=code(env[1])
        two=await c.post('/api/v1/auth/email/request',headers=h,json={'email':'b@example.test'});newcode=code(env[1])
        assert (await c.post('/api/v1/auth/email/verify',headers=h,json={'challenge_id':one.json()['challenge_id'],'code':oldcode})).status_code==400
        env[2].now+=900
        assert (await c.post('/api/v1/auth/email/verify',headers=h,json={'challenge_id':two.json()['challenge_id'],'code':newcode})).status_code==403


async def test_concurrent_verify_is_single_use(env):
    async with client(env) as c:
        h=await bootstrap(c)
        r=await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'})
        body={'challenge_id':r.json()['challenge_id'],'code':code(env[1])}
        responses=await asyncio.gather(*(c.post('/api/v1/auth/email/verify',headers=h,json=body) for _ in range(2)))
        assert sorted(r.status_code for r in responses)==[200,400]
    with sqlite3.connect(env[0].database) as d:assert d.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0]==1


async def test_no_unbound_internal_verification_of_http_challenge(env):
    async with client(env) as c:
        h=await bootstrap(c);r=await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'})
        with pytest.raises(InvalidCode):
            await env[3].state.auth.verify_email_code(r.json()['challenge_id'],code(env[1]),source='203.0.113.42')


@pytest.mark.parametrize('body',[{'email':'a@example.test','source':'spoof'},{'email':'a@example.test','account_id':'foreign'}, {'email':12},{'email':'a\r\nBCC:bad@example.test'},{'email':'x'*300},[],None])
async def test_strict_schema(env,body):
    async with client(env) as c:
        h=await bootstrap(c)
        r=await c.post('/api/v1/auth/email/request',headers=h,content=json.dumps(body))
        # Raw content requires explicit JSON MIME.
        assert r.status_code==415
        r=await c.post('/api/v1/auth/email/request',headers={**h,'Content-Type':'application/json'},content=json.dumps(body))
        assert r.status_code==422 and set(r.json())=={'error'}
        assert 'input' not in r.text and not env[1].messages


async def test_body_limit_chunked_malformed_json_and_foreign_account(env):
    async def chunks():
        yield b'{"email":"';yield b'a'*5000;yield b'"}'
    async with client(env) as c:
        h=await bootstrap(c)
        r=await c.post('/api/v1/auth/email/request',headers={**h,'Content-Type':'application/json'},content=chunks())
        assert r.status_code==413
        assert (await c.post('/api/v1/auth/email/request',headers={**h,'Content-Type':'application/json'},content='x'*5000)).status_code==413
        assert (await c.post('/api/v1/auth/email/request',headers={**h,'Content-Type':'application/json'},content='{')).status_code==422
        await login(c,env)
        assert (await c.get('/api/v1/me?account_id=foreign')).status_code==422


async def test_limits_persist_and_spoofed_proxy_headers_do_not_bypass(env):
    async with client(env) as c:
        h=await bootstrap(c)
        assert (await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'})).status_code==200
        r=await c.post('/api/v1/auth/email/request',headers={**h,'X-Forwarded-For':'1.2.3.4','X-Real-IP':'1.2.3.4'},json={'email':'a@example.test'})
        assert r.status_code==429 and 'retry-after' not in r.headers
    # Recreating app with same DB does not reset budgets.
    restarted=(*env[:3],create_app(env[0],sender=env[1],clock=env[2]))
    async with client(restarted) as c:
        h=await bootstrap(c)
        assert (await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'})).status_code==429


@pytest.mark.parametrize('limit',['source_requests','global_requests'])
async def test_source_and_global_limits(env,limit):
    auth_settings=replace(env[3].state.auth.settings,**{limit:1})
    app=create_app(env[0],sender=env[1],clock=env[2],auth_settings=auth_settings)
    limited=(*env[:3],app)
    async with client(limited) as a,client(limited,'203.0.113.43') as b:
        ah=await bootstrap(a);bh=await bootstrap(b)
        assert (await a.post('/api/v1/auth/email/request',headers=ah,json={'email':'a@example.test'})).status_code==200
        target=a if limit=='source_requests' else b;h=ah if target is a else bh
        assert (await target.post('/api/v1/auth/email/request',headers=h,json={'email':'b@example.test'})).status_code==429


@pytest.mark.parametrize('peer,headers,expected',[
 ('203.0.113.42',{'x-real-ip':'1.2.3.4','x-forwarded-for':'5.6.7.8','forwarded':'for=9.9.9.9'},'203.0.113.42'),
 ('127.0.0.1',{'x-real-ip':'2001:0db8::0001','x-forwarded-for':'5.6.7.8'},'2001:db8::1'),
 ('::ffff:127.0.0.1',{'x-real-ip':'::ffff:192.0.2.1'},'192.0.2.1'),
 ('::1',{'x-real-ip':'192.0.2.5'},'192.0.2.5'),
])
def test_trusted_proxy_and_ip_normalization(env,peer,headers,expected):
    settings=replace(env[0],trusted_proxies=('127.0.0.1/32','::1/128'))
    request=Request({'type':'http','client':(peer,10),'headers':[(k.encode(),v.encode()) for k,v in headers.items()]})
    assert client_source(request,settings)==expected


async def test_sqlite_busy_safe_error_and_health_no_migrations(env,tmp_path):
    async with client(env) as c:
        h=await bootstrap(c)
        with sqlite3.connect(env[0].database) as d:
            d.execute('BEGIN EXCLUSIVE')
            r=await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'})
            assert r.status_code==503 and r.json()=={'error':{'code':'store_unavailable'}}
        assert (await c.get('/api/v1/health/live')).status_code==200
        assert (await c.get('/api/v1/health/ready')).status_code==200
    path=tmp_path/'pending.sqlite3';path.touch();await migrate(path,target=2)
    pending=(replace(env[0],database=path),*env[1:3],create_app(replace(env[0],database=path),sender=env[1],clock=env[2]))
    async with client(pending) as c:
        assert (await c.get('/api/v1/health/ready')).status_code==503
    with sqlite3.connect(path) as d:assert d.execute('SELECT MAX(version) FROM account_schema_migrations').fetchone()[0]==2


async def test_migration_v1_v2_checksums_and_legacy_rows_unchanged(tmp_path):
    path=tmp_path/'synthetic-v2.sqlite3';path.touch()
    with sqlite3.connect(path) as d:d.executescript('CREATE TABLE users(id INTEGER PRIMARY KEY,value TEXT); INSERT INTO users VALUES(1,"unchanged");')
    await migrate(path,target=2)
    with sqlite3.connect(path) as d:before=d.execute('SELECT * FROM account_schema_migrations').fetchall()
    await migrate(path);assert await inspect_schema(path)=='applied'
    with sqlite3.connect(path) as d:
        assert d.execute('SELECT * FROM account_schema_migrations WHERE version<=2').fetchall()==before
        assert d.execute('SELECT * FROM users').fetchall()==[(1,'unchanged')]
        assert d.execute('SELECT MAX(version) FROM account_schema_migrations').fetchone()[0]==5
    assert CHECKSUM==hashlib.sha256('\n'.join(' '.join(v.split()).rstrip(';') for v in DDL.values()).encode()).hexdigest()


async def test_safe_errors_unknown_delivery_and_openapi(env):
    class Uncertain:
        async def send(self,message):raise RuntimeError('SECRET_EMAIL_CODE password /private/path SELECT 1')
    app=create_app(env[0],sender=Uncertain(),clock=env[2])
    async with client((*env[:3],app)) as c:
        h=await bootstrap(c)
        r=await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'})
        assert r.status_code==200 and r.json()['status']=='requested'
        assert not any(x in r.text for x in ['SECRET_EMAIL_CODE','password','/private','SELECT'])
        assert 'Доставка не гарантируется' in r.text
        schema=(await c.get('/openapi.json')).json()
        assert '/api/v1/me' in schema['paths'] and 'ErrorResponse' in schema['components']['schemas']
        for route in ('/api/v1/me','/api/v1/auth/csrf','/not-found'):
            assert (await c.get(route)).headers['cache-control']=='no-store'
    prod=create_app(replace(env[0],environment='production'),sender=Uncertain())
    async with client((*env[:3],prod)) as c:
        assert (await c.get('/docs')).status_code==404
        assert (await c.get('/openapi.json')).status_code==404
    with pytest.raises(ValueError):create_app(replace(env[0],environment='production'),sender=FakeEmailSender())


def test_import_has_no_io_or_bot_imports(tmp_path):
    script='''import socket,sqlite3,os
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network'))
sqlite3.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('database'))
import web_api.app,auth.smtp,web_api.cleanup,sys
assert not {'bot','config','database','maxapi'} & sys.modules.keys()
assert not hasattr(web_api.app,'app')
'''
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


async def test_expired_code_before_browser_expiry_and_logout_csrf(env):
    async with client(env) as c:
        h=await bootstrap(c)
        r=await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'})
        right=code(env[1]);env[2].now+=601
        assert (await c.post('/api/v1/auth/email/verify',headers=h,json={'challenge_id':r.json()['challenge_id'],'code':right})).status_code==400
        # A chosen invalid session cookie is never reused as the newly issued token.
        chosen='a'*43;c.cookies.set(env[0].session_cookie,chosen,domain='app.wish-bot.ru',path='/')
        result,_,_=await login(c,env,'b@example.test')
        assert c.cookies.get(env[0].session_cookie)!=chosen
        assert (await c.post('/api/v1/auth/logout',json={},headers={'Origin':ORIGIN,'X-CSRF-Token':'b'*64})).status_code==403
        assert (await c.get('/api/v1/me')).status_code==200


async def test_untrusted_proxy_spoof_does_not_escape_source_budget(env):
    limited=replace(env[3].state.auth.settings,source_requests=1)
    app=create_app(env[0],sender=env[1],clock=env[2],auth_settings=limited)
    async with client((*env[:3],app)) as c:
        h=await bootstrap(c)
        assert (await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'})).status_code==200
        assert (await c.post('/api/v1/auth/email/request',headers={**h,'X-Real-IP':'192.0.2.99','X-Forwarded-For':'192.0.2.99'},json={'email':'b@example.test'})).status_code==429


async def test_trusted_proxy_requires_one_real_ip_and_records_normalized_source(env):
    from auth.security import digest
    settings=replace(env[0],trusted_proxies=('127.0.0.1/32',))
    app=create_app(settings,sender=env[1],clock=env[2])
    async with client((settings,*env[1:3],app),'127.0.0.1') as c:
        h=await bootstrap(c)
        for extra in ({},{'X-Real-IP':'bad,192.0.2.1'}):
            assert (await c.post('/api/v1/auth/email/request',headers={**h,**extra},json={'email':'a@example.test'})).status_code==403
        assert (await c.post('/api/v1/auth/email/request',headers={**h,'X-Real-IP':'::ffff:192.0.2.1','X-Forwarded-For':'198.51.100.1'},json={'email':'a@example.test'})).status_code==200
    with sqlite3.connect(settings.database) as d:
        hashed=d.execute("SELECT subject_hash FROM auth_rate_limits WHERE event='request_source'").fetchone()[0]
    assert hashed==digest(settings.secret,'source','192.0.2.1')


async def test_expired_browser_context_cleanup_and_no_live_budget_reset(env):
    async with client(env) as c:
        h=await bootstrap(c)
        await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'})
    env[2].now+=86400+901
    await env[3].state.auth.cleanup_expired()
    with sqlite3.connect(env[0].database) as d:
        assert d.execute('SELECT COUNT(*) FROM auth_browser_contexts').fetchone()[0]==0
        assert d.execute('SELECT COUNT(*) FROM auth_browser_challenges').fetchone()[0]==0


async def test_development_cors_is_explicit_and_no_wildcard(env):
    settings=replace(env[0],environment='development',origin='http://127.0.0.1:8091',development_origins=('http://127.0.0.1:5173',),secure_cookies=False)
    class Sender:
        async def send(self,message):raise AssertionError('No email permitted in CORS test')
    app=create_app(settings,sender=Sender())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=settings.origin) as c:
        allowed=await c.options('/api/v1/auth/email/request',headers={'Origin':'http://127.0.0.1:5173','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type,x-csrf-token'})
        assert allowed.status_code==200
        assert allowed.headers['access-control-allow-origin']=='http://127.0.0.1:5173'
        assert allowed.headers['access-control-allow-credentials']=='true'
        assert (await c.options('/api/v1/auth/email/request',headers={'Origin':'https://evil.test','Access-Control-Request-Method':'POST'})).status_code==403


def test_production_browser_settings_cannot_be_weakened(env):
    for changes in ({'secure_cookies':False},{'development_origins':('https://evil.test',)},{'origin':'https://smalltalk.wish-bot.ru'}):
        with pytest.raises(ValueError):replace(env[0],environment='production',**changes)
    with pytest.raises(ValueError):replace(env[0],trusted_proxies=('0.0.0.0/0',))


async def test_unexpected_exception_is_not_reraised_or_logged_with_secrets(env,caplog):
    @env[3].get('/test-failure')
    async def failure():raise RuntimeError('PRIVATE_SQL email@example.test code=123456 token=secret')
    async with client(env) as c:
        r=await c.get('/test-failure')
        assert r.status_code==503 and r.headers['cache-control']=='no-store'
        assert r.json()=={'error':{'code':'service_unavailable'}}
    assert 'Web API internal failure' in caplog.text
    assert all(x not in caplog.text for x in ('PRIVATE_SQL','email@example.test','123456','token=secret'))


async def test_logged_in_browser_must_explicitly_logout_before_new_login(env):
    async with client(env) as c:
        await login(c,env)
        headers=await bootstrap(c)
        r=await c.post('/api/v1/auth/email/request',headers=headers,json={'email':'b@example.test'})
        assert r.status_code==409 and len(env[1].messages)==1


def test_checked_in_openapi_matches_factory(env):
    from pathlib import Path
    exported=json.loads((Path(__file__).resolve().parents[2]/'docs/web-auth-openapi.json').read_text())
    assert exported==env[3].openapi()
    assert 'cookie' in exported['components']['securitySchemes']['sessionCookie']['in']


async def test_nearly_expired_preauth_does_not_send_unusable_code(env):
    async with client(env) as c:
        h=await bootstrap(c);old=c.cookies.get(env[0].preauth_cookie)
        env[2].now+=301
        r=await c.post('/api/v1/auth/email/request',headers=h,json={'email':'a@example.test'})
        assert r.status_code==403 and not env[1].messages
        renewed=await bootstrap(c)
        assert c.cookies.get(env[0].preauth_cookie)!=old and renewed!=h
        assert (await c.post('/api/v1/auth/email/request',headers=renewed,json={'email':'a@example.test'})).status_code==200


async def test_bootstrap_recovers_after_lost_successful_login_response(env):
    async with client(env) as c:
        headers=await bootstrap(c);old=c.cookies.get(env[0].preauth_cookie)
        await login(c,env)
        # Simulate the client losing the response including the new session/clear cookie.
        c.cookies.clear();c.cookies.set(env[0].preauth_cookie,old,domain='app.wish-bot.ru',path='/')
        fresh=await bootstrap(c)
        assert fresh!=headers and c.cookies.get(env[0].preauth_cookie)!=old
        env[2].now+=60
        assert (await c.post('/api/v1/auth/email/request',headers=fresh,json={'email':'a@example.test'})).status_code==200

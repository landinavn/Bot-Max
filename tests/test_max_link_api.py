import re
import sqlite3
from dataclasses import replace
import httpx
import pytest
from test_account_linking import link_env,read
from web_api.app import create_app
from web_api.config import APISettings

ORIGIN='https://app.wish-bot.ru'


def client(e,*,enabled=True):
    settings=APISettings(database=e.path,secret=e.auth.settings.secret,environment='test',max_linking_enabled=enabled,
                         linking_secret=e.settings.secret if enabled else None)
    app=create_app(settings,sender=e.sender,auth_settings=e.auth.settings,clock=e.clock)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('203.0.113.1',1234)),base_url=ORIGIN)


async def logged(c,e,email='alice@example.test'):
    boot=await c.get('/api/v1/auth/csrf');h={'Origin':ORIGIN,'X-CSRF-Token':boot.json()['csrf_token']}
    r=await c.post('/api/v1/auth/email/request',headers=h,json={'email':email});assert r.status_code==200,r.text
    code=re.search(r'Ваш код для входа: ([0-9]+)',e.sender.messages[-1].body)[1]
    r=await c.post('/api/v1/auth/email/verify',headers=h,json={'challenge_id':r.json()['challenge_id'],'code':code});assert r.status_code==200,r.text
    return {'Origin':ORIGIN,'X-CSRF-Token':r.json()['csrf_token']}


async def test_complete_http_flow_and_minimal_identity_payload(link_env):
    e=link_env
    async with client(e) as c:
        h=await logged(c,e)
        assert (await c.get('/api/v1/account/identities')).json()=={'identities':[{'provider':'email','verified':True}]}
        r=await c.post('/api/v1/account/max-link/start',headers=h,json={});assert r.status_code==200,r.text
        start=r.json();key=start['operation']['operation_id']
        assert start['bot_url']=='https://max.ru/se14596751_1_bot' and start['command']=='/link_account'
        assert start['code'] not in start['bot_url'] and r.headers['cache-control']=='no-store'
        status=await c.get('/api/v1/account/max-link/status');assert 'code' not in status.text and status.json()['operation']['status']=='pending'
        assert (await c.post('/api/v1/account/max-link/confirm',headers=h,json={'operation_id':key})).status_code==400
        await e.service.claim_from_max(1001,start['code']);await e.service.confirm_from_max(1001,key)
        assert (await c.get('/api/v1/account/max-link/status')).json()['operation']['max_hint']=='…1001'
        done=await c.post('/api/v1/account/max-link/confirm',headers=h,json={'operation_id':key});assert done.status_code==200,done.text
        assert done.json()['operation']['status']=='completed'
        identities=await c.get('/api/v1/account/identities');assert len(identities.json()['identities'])==2
        assert '1001' not in identities.text and 'alice' not in identities.text and 'account_id' not in identities.text
        contract=c._transport.app.openapi()
        for path,methods in contract['paths'].items():
            if path.startswith('/api/v1/account/'):
                for method,op in methods.items():
                    assert op['security']==[{'sessionCookie':[],**({'csrfHeader':[]} if method=='post' else {})}]


@pytest.mark.parametrize('path',['identities','max-link/status','max-link/start','max-link/confirm','max-link/cancel'])
async def test_no_unauthenticated_access(link_env,path):
    e=link_env
    async with client(e) as c:
        r=await c.get('/api/v1/account/'+path) if path in ('identities','max-link/status') else await c.post('/api/v1/account/'+path,headers={'Origin':ORIGIN},json={} if path.endswith('start') else {'operation_id':'00000000-0000-0000-0000-000000000000'})
        assert r.status_code in (401,403) and r.headers['cache-control']=='no-store'
        assert not read(e.path,'SELECT * FROM account_link_operations')


@pytest.mark.parametrize('case',['csrf','origin','account_id','max_user_id','operation_id','query','oversize'])
async def test_link_start_boundaries(link_env,case):
    e=link_env
    async with client(e) as c:
        h=await logged(c,e);body={};path='/api/v1/account/max-link/start'
        if case=='csrf':h.pop('X-CSRF-Token')
        elif case=='origin':h['Origin']='https://evil.test'
        elif case=='query':path+='?account_id=other'
        elif case=='oversize':body={'extra':'x'*5000}
        else:body={case:'1001'}
        r=await c.post(path,headers=h,json=body)
        assert r.status_code in (403,413,422) and r.headers['cache-control']=='no-store'
        assert not read(e.path,'SELECT * FROM account_link_operations')


async def test_foreign_session_status_and_cancellation(link_env):
    e=link_env
    async with client(e) as a,client(e) as b:
        ha=await logged(a,e);hb=await logged(b,e,'other@example.test')
        start=(await a.post('/api/v1/account/max-link/start',headers=ha,json={})).json();key=start['operation']['operation_id']
        assert (await b.get('/api/v1/account/max-link/status')).json()=={'operation':None}
        assert (await b.post('/api/v1/account/max-link/cancel',headers=hb,json={'operation_id':key})).status_code==400
        assert (await a.post('/api/v1/account/max-link/cancel',headers=ha,json={'operation_id':key})).json()['operation']['status']=='cancelled'


async def test_disable_keeps_email_and_identity_list_without_v4_queries(link_env):
    e=link_env
    with sqlite3.connect(e.path) as d:
        d.execute('DROP TABLE account_link_operations');d.execute('DROP TABLE account_link_limits');d.execute('DELETE FROM account_schema_migrations WHERE version=4')
    async with client(e,enabled=False) as c:
        h=await logged(c,e)
        assert (await c.get('/api/v1/me')).status_code==200
        assert (await c.get('/api/v1/account/identities')).status_code==200
        assert (await c.post('/api/v1/account/max-link/start',headers=h,json={})).json()=={'error':{'code':'linking_disabled'}}
        assert (await c.get('/api/v1/account/max-link/status')).status_code==503


async def test_request_errors_never_include_code_or_identity(link_env,caplog):
    e=link_env
    async with client(e) as c:
        h=await logged(c,e);start=(await c.post('/api/v1/account/max-link/start',headers=h,json={})).json()
        r=await c.post('/api/v1/account/max-link/confirm',headers=h,json={'operation_id':start['operation']['operation_id'],'code':start['code'],'account_id':'private-account'})
        assert r.status_code==422
        assert start['code'] not in r.text and start['code'] not in caplog.text and 'private-account' not in r.text


@pytest.mark.parametrize('setting',[{'linking_secret':b'short'},{'linking_secret':b'fake-email-secret-never-production-00000'},{'max_linking_enabled':'true'}])
def test_unsafe_linking_config_is_rejected(tmp_path,setting):
    settings=dict(database=tmp_path/'test.sqlite3',secret=b'fake-email-secret-never-production-00000',environment='test',max_linking_enabled=True,linking_secret=b'fake-distinct-linking-key-for-tests-only')
    settings.update(setting)
    with pytest.raises(ValueError):APISettings(**settings)

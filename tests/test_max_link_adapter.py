from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
import ssl
import pytest
from maxapi.types import MessageCreated,MessageCallback
from maxapi.context import MemoryContext
from account_linking.max_adapter import create_router,actor_from_event,LinkingStates
from account_linking.bootstrap import configure
from test_account_linking import link_env,started,read
from max_client import MaxAPIConnector
from aiohttp import ClientRequest
from yarl import URL


def event(actor=1001,text='test',*,callback=None,is_bot=False,chat_type='dialog'):
    user={'user_id':actor,'first_name':'Synthetic','is_bot':is_bot}
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()),send_callback=AsyncMock())
    message={'timestamp':1700000000,'sender':user if callback is None else {'user_id':999,'first_name':'Bot','is_bot':True},
             'recipient':{'chat_id':100,'chat_type':chat_type},'body':{'mid':'synthetic-mid','seq':1,'text':text}}
    if callback is not None:
        return MessageCallback(timestamp=1700000000,bot=bot,message=message,callback={'timestamp':1700000000,'callback_id':'synthetic-callback','payload':callback,'user':user})
    return MessageCreated(timestamp=1700000000,bot=bot,message=message)


async def call(router,name,event,context):
    handler=next(x.func_event for x in router.event_handlers if x.func_event.__name__==name)
    await handler(event,context)


async def test_synthetic_private_flow_preserves_product_data_and_uses_callback_actor(link_env):
    e=link_env;s,start=await started(e);router=create_router(e.service);context=MemoryContext(chat_id=100,user_id=1001)
    await context.set_state('existing-product-state');await context.update_data(photo='synthetic-photo',approved_words='keep')
    enter=event(text='/link_account');await call(router,'link_account',enter,context)
    assert await context.get_state()==LinkingStates.waiting_code
    code=event(text=start.code);await call(router,'link_code',code,context)
    assert await context.get_state()=='existing-product-state'
    assert (await context.get_data())['photo']=='synthetic-photo'
    sent=code.bot.send_message.await_args.kwargs
    assert start.code not in sent['text'] and 'a***@e***' in sent['text']
    button=sent['attachments'][0].payload.buttons[0][0]
    callback=event(callback=button.payload);assert actor_from_event(callback)==1001
    await call(router,'link_callback',callback,context)
    assert 'Вернитесь на сайт' in callback.bot.send_message.await_args.kwargs['text']
    assert read(e.path,'SELECT state FROM account_link_operations')[0]['state']=='max_confirmed'
    assert not read(e.path,"SELECT * FROM account_identities WHERE provider='max'")
    assert len(read(e.path,'SELECT * FROM users'))==2


@pytest.mark.parametrize('case',['group','bot','unknown','foreign_callback','forged_object'])
async def test_event_boundary_and_missing_legacy_user(link_env,case):
    e=link_env;s,start=await started(e);router=create_router(e.service);context=MemoryContext(chat_id=100,user_id=1001)
    if case=='forged_object':
        assert actor_from_event(SimpleNamespace(get_ids=lambda:(100,1001))) is None;return
    if case=='foreign_callback':
        await e.service.claim_from_max(1001,start.code)
        value=event(actor=1002,callback='web_link:confirm:'+start.operation.operation_id)
        await call(router,'link_callback',value,context)
    else:
        value=event(actor=3333 if case=='unknown' else 1001,is_bot=case=='bot',chat_type='chat' if case=='group' else 'dialog')
        await call(router,'link_account',value,context)
    assert (await e.service.status(s.token)).status=='pending'
    assert await context.get_state() is None
    assert len(read(e.path,'SELECT * FROM users'))==2


async def test_cancel_restores_state_and_does_not_clear_existing_materials(link_env):
    router=create_router(link_env.service);context=MemoryContext(chat_id=100,user_id=1001)
    await context.set_state('product');await context.update_data(song='synthetic-song')
    await call(router,'link_account',event(),context)
    await call(router,'cancel_input',event(text='/cancel_link'),context)
    assert await context.get_state()=='product' and (await context.get_data())['song']=='synthetic-song'


def test_disabled_bootstrap_has_no_io_and_does_not_add_routes(monkeypatch):
    dispatcher=SimpleNamespace(include_routers=Mock(side_effect=AssertionError('Must not attach')))
    monkeypatch.setattr('account_linking.service.LinkingService',Mock(side_effect=AssertionError('Must not construct')))
    assert configure(dispatcher,SimpleNamespace(max_linking_enabled=False)) is None
    dispatcher.include_routers.assert_not_called()


async def test_linking_transport_requires_certificate_and_hostname_checks():
    async with MaxAPIConnector(verify_ssl=True) as connector:
        context=connector._get_ssl_context(ClientRequest('GET',URL('https://platform-api.max.ru/updates')))
        assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname


def test_configuration_defaults_off(monkeypatch):
    from config import Config
    monkeypatch.setenv('MAX_BOT_TOKEN','synthetic-token');monkeypatch.delenv('MAX_ACCOUNT_LINKING_ENABLED',raising=False)
    assert Config.from_env().max_linking_enabled is False

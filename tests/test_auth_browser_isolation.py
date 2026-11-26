import asyncio
from dataclasses import replace
import sqlite3
import re
import pytest
from auth.errors import InvalidCode,RateLimited
from auth.service import AuthService
from test_account_linking import link_env,login,read


def otp(e):return re.search(r'Ваш код для входа: ([0-9]+)',e.sender.messages[-1].body)[1]
def context(e,name):return (name*64,e.clock.now+900)


async def test_other_browser_cannot_invalidate_first_but_same_browser_can(link_env):
    e=link_env;a=context(e,'a');b=context(e,'b')
    first=await e.auth.request_email_code('a@example.test',source='a',browser_context=a);first_code=otp(e)
    e.clock.now+=60
    second=await e.auth.request_email_code('a@example.test',source='b',browser_context=b);second_code=otp(e)
    assert read(e.path,'SELECT invalidated_at FROM auth_challenges WHERE challenge_id=?',(first.challenge_id,))==[{'invalidated_at':None}]
    e.clock.now+=60
    third=await e.auth.request_email_code('a@example.test',source='b',browser_context=b)
    assert read(e.path,'SELECT invalidated_at FROM auth_challenges WHERE challenge_id=?',(second.challenge_id,))[0]['invalidated_at'] is not None
    result=await e.auth.verify_email_code(first.challenge_id,first_code,source='a',browser_context=a)
    assert result.account
    with pytest.raises(InvalidCode):await e.auth.verify_email_code(third.challenge_id,otp(e),source='b',browser_context=b)


async def test_wrong_browser_and_legacy_do_not_spend_victim_attempts(link_env):
    e=link_env;a=context(e,'a');b=context(e,'b')
    r=await e.auth.request_email_code('a@example.test',source='a',browser_context=a);value=otp(e)
    for ctx in [b,None]*4:
        with pytest.raises(InvalidCode):await e.auth.verify_email_code(r.challenge_id,value,source='attacker',browser_context=ctx)
    assert read(e.path,'SELECT attempts,locked_at FROM auth_challenges WHERE challenge_id=?',(r.challenge_id,))==[{'attempts':0,'locked_at':None}]
    assert not read(e.path,"SELECT * FROM auth_rate_limits WHERE event='attempt_email'")
    assert len(read(e.path,"SELECT * FROM auth_rate_limits WHERE event='attempt_global'"))==8
    await e.auth.verify_email_code(r.challenge_id,value,source='a',browser_context=a)


async def test_legacy_request_only_invalidates_legacy_codes(link_env):
    e=link_env;a=context(e,'a');r=await e.auth.request_email_code('a@example.test',source='a',browser_context=a);value=otp(e)
    e.clock.now+=60;await e.auth.request_email_code('a@example.test',source='legacy')
    await e.auth.verify_email_code(r.challenge_id,value,source='a',browser_context=a)


async def test_active_cap_and_concurrent_requests_preserve_foreign_challenge(link_env):
    e=link_env;settings=replace(e.auth.settings,resend_interval=1,active_email_challenges=2)
    service=AuthService(e.path,settings,e.sender,clock=e.clock)
    first=await service.request_email_code('a@example.test',source='a',browser_context=context(e,'a'));value=otp(e)
    e.clock.now+=1
    results=await asyncio.gather(*(service.request_email_code('a@example.test',source=x,browser_context=context(e,x)) for x in ('b','c')),return_exceptions=True)
    assert sum(isinstance(x,RateLimited) for x in results)==1
    assert len(read(e.path,'SELECT * FROM auth_challenges WHERE invalidated_at IS NULL AND consumed_at IS NULL'))==2
    await service.verify_email_code(first.challenge_id,value,source='a',browser_context=context(e,'a'))


@pytest.mark.parametrize('limit',['email_requests','source_requests','global_requests','active_global_challenges'])
async def test_email_request_limits_still_apply_across_browsers(link_env,limit):
    e=link_env;s=AuthService(e.path,replace(e.auth.settings,**{limit:1}),e.sender,clock=e.clock)
    await s.request_email_code('a@example.test',source='same',browser_context=context(e,'a'));e.clock.now+=60
    with pytest.raises(RateLimited):await s.request_email_code('a@example.test',source='same',browser_context=context(e,'b'))


async def test_shared_email_attempt_budget_cannot_be_reset_by_resend(link_env):
    e=link_env;settings=replace(e.auth.settings,email_attempts=2);s=AuthService(e.path,settings,e.sender,clock=e.clock)
    for name in ('a','b'):
        r=await s.request_email_code('a@example.test',source=name,browser_context=context(e,name))
        with pytest.raises(InvalidCode):await s.verify_email_code(r.challenge_id,'invalid',source=name,browser_context=context(e,name))
        e.clock.now+=60
    r=await s.request_email_code('a@example.test',source='c',browser_context=context(e,'c'))
    with pytest.raises(RateLimited):await s.verify_email_code(r.challenge_id,otp(e),source='c',browser_context=context(e,'c'))


async def test_concurrent_global_active_cap_is_atomic(link_env):
    e=link_env;s=AuthService(e.path,replace(e.auth.settings,active_global_challenges=1),e.sender,clock=e.clock)
    results=await asyncio.gather(*(s.request_email_code(x+'@example.test',source=x,browser_context=context(e,x)) for x in ('a','b')),return_exceptions=True)
    assert sum(isinstance(x,RateLimited) for x in results)==1
    assert len(read(e.path,'SELECT * FROM auth_challenges'))==1


async def test_email_active_cap_rejects_fourth_browser_without_invalidating_existing(link_env):
    e=link_env
    for name in ('a','b','c'):
        await e.auth.request_email_code('a@example.test',source=name,browser_context=context(e,name));e.clock.now+=60
    with pytest.raises(RateLimited):await e.auth.request_email_code('a@example.test',source='d',browser_context=context(e,'d'))
    assert len(read(e.path,'SELECT * FROM auth_challenges WHERE invalidated_at IS NULL'))==3

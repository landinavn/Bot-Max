"""Budget removal does not remove authorisation or unknown-submission safeguards."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from multstudio import repository as repo
from multstudio.worker import Worker
from multstudio.router import create_router
from test_multstudio import studio
from test_multstudio_episode_three import ready,attach_audio
from test_multstudio_project_menu import event
from test_handlers import dispatch_callback,ready_context


async def test_all_paid_kinds_allow_missing_price_and_zero_project_budget(studio):
    project,settings,_=studio
    settings=replace(settings,image_price=0,video_price=0,speech_price=0,speech_v3_price=0,heygen_price=0)
    char=(await repo.children('character',project))[0]
    image=await repo.quote('character',char['id'],'image',settings)
    await repo.confirm(image['id'],456)
    shot=await attach_audio(studio,await ready(studio))
    for kind in ('speech','video'):
        item=await repo.quote('scene',shot['id'],kind,settings)
        assert item['estimate']==0
        assert (await repo.confirm(item['id'],456))['status']=='queued'
    assert (await repo.budget(project))['limit']==0
    assert (await repo.job(image['id']))['approved_by']==456


async def test_unknown_paid_post_still_requires_task_review_but_not_cost_reconciliation(studio):
    project,settings,_=studio
    snap=await attach_audio(studio,await ready(studio))
    video=await repo.quote('scene',snap['id'],'video',settings)
    await repo.confirm(video['id'],456)
    await repo.update_job(video['id'],status='manual',provider_id='known-task',actual=None,error='unknown_submission')
    with pytest.raises(ValueError):await repo.resolve_video(video['id'],999)
    await repo.resolve_video(video['id'],456)
    row=await repo.job(video['id'])
    assert row['status']=='failed' and row['actual'] is None
    assert await repo.claim() is None


async def test_zero_budget_does_not_authorise_unconfirmed_job(studio):
    char=(await repo.children('character',studio[0]))[0]
    j=await repo.quote('character',char['id'],'image',replace(studio[1],image_price=0))
    assert await repo.claim() is None
    with pytest.raises(ValueError,match='подтверждения'):await repo.begin_submission(j['id'])
    with pytest.raises(ValueError,match='доступа'):await repo.confirm(j['id'],999)


async def test_old_budget_and_cost_buttons_do_not_open_money_input(studio):
    project,settings,assets=studio
    router=create_router(456,settings,assets)
    for payload in ('budget:'+project,'cost:old-job'):
        e=event('studio:'+payload);context=await ready_context()
        await dispatch_callback(router,e,context)
        assert 'больше не используются' in e.bot.send_message.await_args.kwargs['text']
        assert await context.get_state() is None

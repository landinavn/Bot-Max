"""Preflight diagnostics and exact USD → micro-USD parsing; no paid requests."""
import base64
import json
import logging
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from PIL import Image

from config import Config
from multstudio import repository as repo
from multstudio.adapters import Providers
from multstudio.settings import Settings, money, price_from_env, dollars
from multstudio.worker import Worker
from test_multstudio import studio, queued_image


@pytest.mark.parametrize('value,expected', [
    ('0.04',40000),('0,04',40000),(' 0,04 ',40000),('0.040000',40000),
    ('',0),('   ',0),('0',0),('1',1000000),('0.000001',1),('0,0000001',1),
])
@pytest.mark.parametrize('name',['STUDIO_IMAGE_USD','STUDIO_KLING_5S_USD','STUDIO_TTS_1000_CHARS_USD'])
def test_all_prices_accept_comma_spaces_blank_and_exact_precision(monkeypatch,name,value,expected):
    monkeypatch.setenv(name,value)
    assert price_from_env(name)==expected
    assert type(price_from_env(name)) is int


@pytest.mark.parametrize('value',['bad','0,0.4','NaN','Infinity','-0,04','100001'])
def test_bad_price_fails_with_variable_name_without_coercing_to_zero(monkeypatch,value):
    monkeypatch.setenv('STUDIO_IMAGE_USD',value)
    with pytest.raises(ValueError,match='STUDIO_IMAGE_USD'):
        price_from_env('STUDIO_IMAGE_USD')


def test_settings_load_all_three_normalized_prices(monkeypatch,tmp_path):
    config=Config(token='fake',photo_path=tmp_path/'missing-reference.jpg',image_model='test-model')
    monkeypatch.setenv('STUDIO_IMAGE_USD',' 0,04 ')
    monkeypatch.setenv('STUDIO_KLING_5S_USD',' 0,50 ')
    monkeypatch.setenv('STUDIO_TTS_1000_CHARS_USD',' ')
    settings=Settings.from_config(config)
    assert (settings.image_price,settings.video_price,settings.speech_price)==(40000,500000,0)


async def test_preflight_value_error_has_complete_stack_and_cause_before_any_paid_call(studio,monkeypatch,caplog):
    project,settings,assets=studio
    item=await queued_image(studio)
    config=Config(token='fake',photo_path=Path('unused'),image_api_key='fake-image',
                  storage_bucket='bucket',storage_access_key_id='fake-access',storage_secret_access_key='fake-secret')
    monkeypatch.setattr('image_service._storage_client',lambda config: object())
    providers=Providers(settings,config,assets)
    providers.image=AsyncMock()
    worker=Worker(SimpleNamespace(),456,settings,providers,assets)
    with caplog.at_level(logging.ERROR,logger='multstudio.worker'):
        await worker.execute(await repo.claim())
    assert 'Preflight failed:' in caplog.text and item['id'] in caplog.text
    assert 'Traceback (most recent call last)' in caplog.text
    assert 'multstudio/adapters.py' in caplog.text and 'in preflight' in caplog.text
    assert 'ValueError: Задайте STORAGE_PUBLIC_BASE_URL' in caplog.text
    assert (await repo.job(item['id']))['status']=='preflight_failed'
    assert (await repo.budget(project))['committed']==0
    providers.image.assert_not_awaited()


async def test_traceback_redacts_configured_keys_including_chained_exception(studio,caplog):
    _,settings,assets=studio
    settings=replace(settings,kling_key='private-piapi-key',tts_key='private-tts-key')
    item=await queued_image(studio)
    def fail(kind):
        try:
            raise RuntimeError('private-storage-key')
        except RuntimeError as cause:
            raise ValueError('private-piapi-key private-tts-key private-openai-key') from cause
    config=SimpleNamespace(image_api_key='private-openai-key',storage_secret_access_key='private-storage-key')
    providers=SimpleNamespace(preflight=fail,config=config,image=AsyncMock())
    with caplog.at_level(logging.ERROR,logger='multstudio.worker'):
        await Worker(SimpleNamespace(),456,settings,providers,assets).execute(await repo.claim())
    assert 'Traceback' in caplog.text and 'ValueError' in caplog.text and 'RuntimeError' in caplog.text
    assert '[REDACTED]' in caplog.text
    for key in ('private-storage-key','private-piapi-key','private-tts-key','private-openai-key'):
        assert key not in caplog.text
    assert (await repo.job(item['id']))['status']=='preflight_failed'


@pytest.mark.parametrize('assets_value',[{}, {'image':None}, {'image':{}}])
async def test_first_character_master_uses_text_only_and_never_opens_reference(studio,monkeypatch,assets_value):
    project,settings,assets=studio
    char=(await repo.children('character',project))[0]
    snap=await repo.snapshot('character',char['id'])
    snap['assets']=assets_value
    snap['quote']=settings.quote('image',snap)
    config=Config(token='fake',photo_path=Path('/nonexistent/first-reference.jpg'),
                  image_api_key='fake-openai',image_model='test-model')
    providers=Providers(settings,config,assets)
    assets.asset_url=AsyncMock(side_effect=AssertionError('No reference should be published'))
    edits=AsyncMock(side_effect=AssertionError('Edits must not be used without a reference'))
    monkeypatch.setattr('multstudio.adapters._generate_image_bytes',edits)
    buf=BytesIO(); Image.new('RGB',(8,12),'white').save(buf,format='PNG'); png=buf.getvalue()
    with respx.mock() as api:
        generated=api.post('https://api.openai.com/v1/images/generations').mock(return_value=httpx.Response(200,
            json={'data':[{'b64_json':base64.b64encode(png).decode()}]}))
        assert await providers.image(snap)==png
    request=json.loads(generated.calls[0].request.content)
    assert request['prompt'] and 'image' not in request
    assert generated.call_count==1
    edits.assert_not_awaited()
    assets.asset_url.assert_not_awaited()


async def test_004_usd_estimate_and_budget_match_as_integer_40000_micros(studio):
    project,settings,_=studio
    settings=replace(settings,image_price=money('0,04'))
    char=(await repo.children('character',project))[0]
    item=await repo.quote('character',char['id'],'image',settings)
    assert item['estimate']==40000 and type(item['estimate']) is int
    await repo.budget(project,money('0.039999'))
    with pytest.raises(ValueError,match='бюджета'):
        await repo.confirm(item['id'],456)
    await repo.budget(project,money('0,04'))
    await repo.confirm(item['id'],456)
    assert (await repo.budget(project))=={'limit':40000,'committed':40000}
    assert dollars(40000)=='0.040000 USD'
    async with repo.connect() as db:
        row=await repo.one(db,'''SELECT typeof(p.budget) AS budget_type,typeof(j.estimate) AS estimate_type
            FROM cartoon_jobs j JOIN cartoon_projects p ON p.id=j.project_id WHERE j.id=?''',(item['id'],))
    assert row=={'budget_type':'integer','estimate_type':'integer'}

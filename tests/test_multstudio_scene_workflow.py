import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from multstudio import repository as repo
from multstudio.locations import use_previous_location
from multstudio.prompts import scene_image_prompt, validate_scene_image_references
from multstudio.router import create_router, StudioStates
from multstudio.uploads import upload_scene_image
from test_multstudio import studio, ready_scene, add_image, queued_image
from test_multstudio_character_description import replies
from test_multstudio_project_menu import event
from test_handlers import call_handler, dispatch_callback, message_event, ready_context


@pytest.mark.parametrize('before,after,keep',[
    ('Гостиная. Дима на диване.','Аня сидит на диване.',True),
    ('Гостиная. Дима на диване.','Кухня. Аня у стола.',False),
    ('Кухня. Аня у стола.','На кухне Маша смотрит на маму.',True),
    ('Кухня. Аня у стола.','На улице идёт снег.',False),
    ('Кухня.','Аня входит в новую комнату.',False),
    ('У окна.','Кухня.',False),
    ('Кухня-гостиная.','Кухня-гостиная.',True),
])
def test_room_change_detection(before,after,keep):
    assert use_previous_location({'visual':before},{'visual':after}) is keep


def test_room_change_in_title_is_respected():
    assert not use_previous_location({'visual':'Гостиная.'},{'title':'Кухня','visual':'Аня у стола.'})


async def scene_rows(studio):
    episode=(await repo.children('episode',studio[0]))[0]
    return await repo.children('scene',episode['id'])


async def test_previous_scene_not_first_scene_is_room_reference(studio):
    _,_,assets=studio
    await ready_scene(studio)
    scenes=await scene_rows(studio)
    ids=[]
    expected=[False,False,True,False,True,True,True]
    for index,scene in enumerate(scenes):
        snap=await repo.snapshot('scene',scene['id'])
        assert bool(snap.get('location_reference_required')) is expected[index]
        if expected[index]:
            assert snap['location_reference']['id']==ids[index-1]
        else:
            assert not snap.get('location_reference')
        ids.append(await add_image(assets,snap))
    assert (await repo.snapshot('scene',scenes[2]['id']))['location_reference']['id']==ids[1]


async def test_same_room_blocks_unapproved_previous_but_new_room_can_be_quoted(studio):
    _,settings,assets=studio
    await ready_scene(studio)
    scenes=await scene_rows(studio)
    # Scene two starts a living-room set; scene three continues it.
    assert (await repo.quote('scene',scenes[1]['id'],'image',settings))['status']=='quoted'
    await add_image(assets,await repo.snapshot('scene',scenes[1]['id']),approved=False)
    with pytest.raises(ValueError,match='предыдущей сцены'):
        await repo.quote('scene',scenes[2]['id'],'image',settings)


def test_preflight_rejects_missing_unapproved_location_and_keeps_character_refs():
    snap=dict(target='scene',number=3,cast=[{'assets':{'image':{'approved':1}}}],
              location_reference_required=True)
    with pytest.raises(ValueError,match='предыдущей сцены'):
        validate_scene_image_references(snap)
    snap['location_reference']={'approved':0}
    with pytest.raises(ValueError): validate_scene_image_references(snap)
    snap['location_reference']['approved']=1
    validate_scene_image_references(snap)
    snap['cast'][0]['assets']['image']['approved']=0
    with pytest.raises(ValueError,match='персонажей'): validate_scene_image_references(snap)


@pytest.mark.parametrize('legacy',[False,True])
async def test_plain_scene_edit_changes_only_visual_from_current_database(studio,replies,legacy):
    scene=(await scene_rows(studio))[2]
    context=await ready_context()
    router=create_router(456,studio[1],studio[2])
    if legacy:
        await context.update_data(studio_input=dict(action='edit',entity='scene',key=scene['id']))
        await context.set_state(StudioStates.input)
    else:
        pressed=event('studio:edit:scene:'+scene['id'])
        await dispatch_callback(router,pressed,context)
        assert 'Опишите словами' in pressed.bot.send_message.await_args.kwargs['text']
    current=dict(scene['spec'],extra={'keep':1},duration=8)
    await repo.edit('scene',scene['id'],current)
    await call_handler(router,'studio_input',message_event('Аня сидит на диване.'),context)
    saved=await repo.get('scene',scene['id'])
    assert saved['spec']==dict(current,visual='Аня сидит на диване.')
    assert await context.get_state()!=StudioStates.input
    assert await repo.claim() is None


@pytest.mark.parametrize('value',['','x'*10001])
async def test_invalid_visual_keeps_scene_and_waiting_state(studio,replies,value):
    scene=(await scene_rows(studio))[0]
    context=await ready_context()
    router=create_router(456,studio[1],studio[2])
    await dispatch_callback(router,event('studio:edit:scene:'+scene['id']),context)
    await call_handler(router,'studio_input',message_event(value),context)
    assert (await repo.get('scene',scene['id']))['spec']==scene['spec']
    assert await context.get_state()==StudioStates.input


def png():
    output=io.BytesIO()
    Image.new('RGB',(32,48),'red').save(output,format='JPEG')
    return output.getvalue()


def fake_download(assets,monkeypatch,data=None):
    async def download(url,suffix,**kwargs):
        kwargs.pop('limit',None)
        kwargs.pop('accept',None)
        return await assets.write(png() if data is None else data,suffix,**kwargs)
    monkeypatch.setattr(assets,'download',download)


def fake_s3(assets,monkeypatch,*,fail=False):
    # Keep real asset_url publication/DB binding and assert upload bytes exist.
    assets.config=SimpleNamespace(storage_bucket='vision-board-bot-images',
                                  storage_endpoint_url='https://storage.yandexcloud.net')
    calls=[]
    async def publish(relative):
        calls.append(relative)
        assert assets.path(relative).read_bytes().startswith(b'\x89PNG')
        if fail: raise RuntimeError('S3 unavailable')
        return assets.canonical_image_url(assets.object_key(relative))
    monkeypatch.setattr(assets,'publish',publish)
    return calls


async def test_upload_is_free_approved_png_and_kling_uses_uploaded_not_generated_image(studio,monkeypatch):
    _,settings,assets=studio
    scene,_=await ready_scene(studio)
    old=await add_image(assets,await repo.snapshot('scene',scene['id']))
    fake_download(assets,monkeypatch)
    calls=fake_s3(assets,monkeypatch)
    job_id,url=await upload_scene_image(assets,scene['id'],456,'https://cdn.example/client.jpg')
    item=await repo.job(job_id)
    asset=await repo.job_result(job_id)
    assert item['kind']=='scene_image' and item['status']=='done'
    assert item['actual']==item['estimate']==0
    assert item['result_url']==url==asset['public_url']
    assert asset['approved']==1 and asset['id']!=old
    assert asset['path']==f'{job_id}/image.png'
    assert calls==[f'{job_id}/image.png']
    assert assets.path(f'{job_id}/source.bin').exists()
    # A previously running generator may finish later; it must not replace the upload.
    late=await add_image(assets,await repo.snapshot('scene',scene['id']),approved=False)
    quote=await repo.quote('scene',scene['id'],'video',settings)
    assert json.loads(quote['snapshot'])['assets']['image']['id']==asset['id']
    assert (await repo.budget(studio[0]))['committed']==0
    assert await repo.claim() is None
    # Explicitly approving a different result still lets the administrator switch.
    await repo.approve_asset(late,456)
    assert (await repo.snapshot('scene',scene['id']))['assets']['image']['id']==late


@pytest.mark.parametrize('bad_image',[False,True])
async def test_failed_upload_preserves_previous_approved_image(studio,monkeypatch,bad_image):
    _,_,assets=studio
    scene,_=await ready_scene(studio)
    old=await add_image(assets,await repo.snapshot('scene',scene['id']))
    fake_download(assets,monkeypatch,b'bad' if bad_image else None)
    fake_s3(assets,monkeypatch,fail=True)
    with pytest.raises(ValueError):
        await upload_scene_image(assets,scene['id'],456,'https://cdn.example/client.jpg')
    assert (await repo.snapshot('scene',scene['id']))['assets']['image']['id']==old
    async with repo.connect() as db:
        job=await repo.one(db,"SELECT * FROM cartoon_jobs WHERE kind='scene_image'")
    assert job['status']=='upload_failed' and job['actual']==0


async def test_upload_router_accepts_photo_without_caption_and_offers_paid_video_quote(studio,replies,monkeypatch):
    scene=(await scene_rows(studio))[0]
    context=await ready_context()
    router=create_router(456,studio[1],studio[2])
    await dispatch_callback(router,event('studio:scene_upload:'+scene['id']),context)
    service=AsyncMock(return_value=('job','https://cdn.example/image.png'))
    monkeypatch.setattr('multstudio.uploads.upload_scene_image',service)
    submitted=message_event(None)
    submitted.message.body.attachments=[SimpleNamespace(type='image',payload=SimpleNamespace(url='https://cdn.example/source.jpg'))]
    await call_handler(router,'studio_input',submitted,context)
    service.assert_awaited_once_with(studio[2],scene['id'],456,'https://cdn.example/source.jpg')
    text=submitted.message.answer.await_args.args[0]
    assert 'Изображение загружено и утверждено' in text
    keyboard=submitted.message.answer.await_args.kwargs['attachments']
    assert len(keyboard)==1
    assert 'studio:make:video:scene:'+scene['id'] in str(keyboard)
    assert await context.get_state()!=StudioStates.input


async def test_unauthorized_upload_is_rejected_before_download(studio,monkeypatch):
    scene=(await scene_rows(studio))[0]
    download=AsyncMock()
    monkeypatch.setattr(studio[2],'download',download)
    with pytest.raises(ValueError,match='доступ'):
        await upload_scene_image(studio[2],scene['id'],999,'https://cdn.example/photo.jpg')
    download.assert_not_awaited()


async def test_missing_location_stops_legacy_job_before_paid_submission(studio,monkeypatch):
    from multstudio.worker import Worker
    item=await queued_image(studio)
    scene=(await scene_rows(studio))[2]
    await ready_scene(studio)
    snap=await repo.snapshot('scene',scene['id'])
    snap['quote']={'model':'image-test','image_quality':'high'}
    await repo.update_job(item['id'],status='queued')
    async with repo.connect() as db:
        await db.execute('UPDATE cartoon_jobs SET snapshot=? WHERE id=?',(repo.encoded(snap),item['id']))
        await db.commit()
    item=await repo.claim()
    providers=SimpleNamespace(preflight=lambda kind:None,image=AsyncMock())
    begin=AsyncMock()
    monkeypatch.setattr(repo,'begin_submission',begin)
    await Worker(SimpleNamespace(),456,studio[1],providers,studio[2]).execute(item)
    begin.assert_not_awaited()
    providers.image.assert_not_awaited()
    assert (await repo.job(item['id']))['status']=='preflight_failed'


async def test_edits_request_contains_both_characters_and_previous_room_bytes(studio,monkeypatch):
    import base64
    from pathlib import Path
    import httpx
    import respx
    from aioresponses import aioresponses
    from config import Config
    from multstudio.adapters import Providers
    _,settings,assets=studio
    await ready_scene(studio)
    scenes=await scene_rows(studio)
    await add_image(assets,await repo.snapshot('scene',scenes[1]['id']))
    snap=await repo.snapshot('scene',scenes[2]['id'])
    snap['quote']=settings.quote('image',snap)
    urls=['https://cdn.example/anya.png','https://cdn.example/dima.png','https://cdn.example/room.png']
    assets.asset_url=AsyncMock(side_effect=urls)
    images=[]
    for colour in ('red','blue','green'):
        buffer=io.BytesIO()
        Image.new('RGB',(16,24),colour).save(buffer,format='PNG')
        images.append(buffer.getvalue())
    config=Config(token='fake',photo_path=Path('unused'),image_api_key='fake',image_model='image-test')
    with aioresponses() as downloads,respx.mock() as api:
        for url,data in zip(urls,images): downloads.get(url,body=data)
        route=api.post('https://api.openai.com/v1/images/edits').mock(return_value=httpx.Response(200,
            json={'data':[{'b64_json':base64.b64encode(images[0]).decode()}]}))
        await Providers(settings,config,assets).image(snap)
    assert route.call_count==1
    content=route.calls[0].request.content
    for data in images: assert data in content
    assert b'Keep the exact same background, furniture, and room interior' in content
    assert urls[-1].encode() in content

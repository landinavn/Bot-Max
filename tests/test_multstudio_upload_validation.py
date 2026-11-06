import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx
from PIL import Image

from multstudio import repository as repo
from multstudio.router import create_router, StudioStates
from multstudio.uploads import png_bytes, upload_scene_image
from test_multstudio import studio, ready_scene, add_image
from test_multstudio_character_description import replies
from test_multstudio_project_menu import event
from test_handlers import call_handler, dispatch_callback, message_event, ready_context


def image_bytes(fmt='JPEG',size=(941,1672),**options):
    output=io.BytesIO()
    Image.new('RGB',size,'cornflowerblue').save(output,format=fmt,**options)
    return output.getvalue()


@pytest.mark.parametrize('filename',['source.bin','photo.jpg','photo.jpeg','photo.png','without_extension'])
@pytest.mark.parametrize('fmt',['JPEG','PNG'])
def test_actual_content_not_extension_controls_validation(tmp_path,filename,fmt):
    path=tmp_path/filename
    path.write_bytes(image_bytes(fmt))
    output=Image.open(io.BytesIO(png_bytes(path)))
    assert output.format=='PNG' and output.size==(941,1672) and output.mode=='RGB'


def test_original_jpg_941_by_1672_is_accepted_and_logged(tmp_path,caplog):
    path=tmp_path/'source.bin'
    path.write_bytes(image_bytes(progressive=True))
    assert path.stat().st_size<1024*1024
    with caplog.at_level(logging.INFO,logger='multstudio.uploads'):
        png_bytes(path,job_id='test-job',scene_id='test-scene')
    assert "'format': 'JPEG'" in caplog.text
    assert "'pixels': 1573352" in caplog.text
    assert 'test-job' in caplog.text and 'test-scene' in caplog.text


@pytest.mark.parametrize('fmt',['WEBP','GIF','BMP'])
def test_other_real_formats_remain_rejected_even_named_jpg(tmp_path,caplog,fmt):
    path=tmp_path/'photo.jpg'
    path.write_bytes(image_bytes(fmt))
    with caplog.at_level(logging.WARNING,logger='multstudio.uploads'),pytest.raises(ValueError,match=fmt):
        png_bytes(path)
    assert 'reason=unsupported_format' in caplog.text
    assert 'reason=pixel_limit' not in caplog.text
    assert "'pixels': 1573352" in caplog.text


def test_pixel_limit_accepts_exactly_40_million_and_rejects_excess(tmp_path,caplog):
    # Header is real PNG; reject over-limit before allocating decoded RGB pixels.
    path=tmp_path/'large.png'
    Image.new('L',(8000,5000)).save(path)
    assert Image.open(io.BytesIO(png_bytes(path))).size==(8000,5000)
    Image.new('L',(8000,5001)).save(path)
    with caplog.at_level(logging.WARNING,logger='multstudio.uploads'),pytest.raises(ValueError,match='40008000'):
        png_bytes(path)
    assert 'reason=pixel_limit' in caplog.text and 'limit=40000000' in caplog.text


@pytest.mark.parametrize('data',[b'<html>not an image</html>',b'\xff\xd8\xff\xe0invalid',image_bytes()[:100]])
def test_invalid_or_truncated_content_is_rejected_and_logged(tmp_path,caplog,data):
    path=tmp_path/'valid-name.jpg'
    path.write_bytes(data)
    with caplog.at_level(logging.WARNING,logger='multstudio.uploads'),pytest.raises(ValueError):
        png_bytes(path)
    assert 'reason=decode_failed' in caplog.text


@pytest.mark.parametrize('fmt,mime',[('JPEG','application/octet-stream'),('PNG','image/jpeg')])
async def test_real_download_to_scene_s3_and_video_quote_941_by_1672(studio,monkeypatch,caplog,fmt,mime):
    _,settings,assets=studio
    scene,_=await ready_scene(studio)
    source=image_bytes(fmt)
    assets.config=SimpleNamespace(storage_bucket='vision-board-bot-images',
        storage_endpoint_url='https://storage.yandexcloud.net',
        storage_public_base_url='https://storage.yandexcloud.net/vision-board-bot-images')
    s3=Mock()
    monkeypatch.setattr('image_service._storage_client',lambda config:s3)
    monkeypatch.setattr('multstudio.assets.public_url',AsyncMock())
    url='https://cdn.example/original.bin?token=never-log-this'
    with respx.mock() as api,caplog.at_level(logging.INFO):
        route=api.get(url).mock(return_value=httpx.Response(200,content=source,headers={'Content-Type':mime}))
        job_id,public=await upload_scene_image(assets,scene['id'],456,url)
    assert route.call_count==1
    assert route.calls[0].request.headers['Accept']=='image/jpeg, image/png'
    assert all('never-log-this' not in record.getMessage()
               for record in caplog.records if record.name.startswith('multstudio.'))
    assert assets.path(f'{job_id}/source.bin').read_bytes()==source
    result=assets.path(f'{job_id}/image.png')
    with Image.open(result) as im:
        assert im.format=='PNG' and im.size==(941,1672)
    s3.put_object.assert_called_once()
    args=s3.put_object.call_args.kwargs
    assert args['Key']==f'studio/{job_id}/image.png'
    assert args['Body']==result.read_bytes() and args['ContentType']=='image/png'
    assert public==f'https://storage.yandexcloud.net/vision-board-bot-images/studio/{job_id}/image.png'
    item=await repo.job(job_id)
    asset=await repo.job_result(job_id)
    assert item['kind']=='scene_image' and item['status']=='done' and item['actual']==0
    assert asset['approved']==1 and asset['public_url']==public
    assert (await repo.snapshot('scene',scene['id']))['assets']['image']['id']==asset['id']
    quote=await repo.quote('scene',scene['id'],'video',settings)
    assert quote['status']=='quoted'
    assert await repo.claim() is None


async def test_webp_disguised_as_jpeg_from_max_is_not_converted_or_selected(studio,monkeypatch,caplog):
    _,_,assets=studio
    scene,_=await ready_scene(studio)
    old=await add_image(assets,await repo.snapshot('scene',scene['id']))
    monkeypatch.setattr('multstudio.assets.public_url',AsyncMock())
    publish=AsyncMock()
    monkeypatch.setattr(assets,'publish',publish)
    with respx.mock() as api,caplog.at_level(logging.INFO):
        api.get('https://cdn.example/photo.jpg').mock(return_value=httpx.Response(200,
            content=image_bytes('WEBP'),headers={'Content-Type':'image/jpeg'}))
        with pytest.raises(ValueError,match='WEBP'):
            await upload_scene_image(assets,scene['id'],456,'https://cdn.example/photo.jpg')
    assert 'reason=unsupported_format' in caplog.text
    publish.assert_not_awaited()
    assert (await repo.snapshot('scene',scene['id']))['assets']['image']['id']==old


@pytest.mark.parametrize('kind',['image','file'])
async def test_max_attachment_photo_and_file_both_reach_content_validator(studio,replies,monkeypatch,kind):
    scene,_=await ready_scene(studio)
    context=await ready_context()
    router=create_router(456,studio[1],studio[2])
    pressed=event('studio:scene_upload:'+scene['id'])
    await dispatch_callback(router,pressed,context)
    assert '«Файл»' in pressed.bot.send_message.await_args.kwargs['text']
    service=AsyncMock(return_value=('job','https://cdn.example/result.png'))
    monkeypatch.setattr('multstudio.uploads.upload_scene_image',service)
    submitted=message_event(None)
    from maxapi.types.attachments.attachment import Attachment
    payload={'url':'https://cdn.example/source.jpg'}
    if kind=='image': payload.update(photo_id=1,token='secret')
    attachment=Attachment(type=kind,payload=payload)
    submitted.message.body.attachments=[attachment]
    await call_handler(router,'studio_input',submitted,context)
    service.assert_awaited_once_with(studio[2],scene['id'],456,'https://cdn.example/source.jpg')
    assert await context.get_state()!=StudioStates.input

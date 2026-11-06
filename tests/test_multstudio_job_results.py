"""Job-specific persisted results, canonical S3 links and MAX image attachments."""
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from maxapi.types import AttachmentUpload

from multstudio import repository as repo
from multstudio.router import create_router
from multstudio.worker import Worker
from multstudio.assets import Assets
from test_handlers import dispatch_callback, ready_context
from test_multstudio import studio
from test_multstudio_project_menu import event

PREFIX='https://storage.yandexcloud.net/vision-board-bot-images/'


def png(color='navy'):
    buf=BytesIO(); Image.new('RGB',(12,18),color).save(buf,format='PNG')
    return buf.getvalue()


@pytest.fixture
def result_assets(studio):
    assets=studio[2]
    assets.config=SimpleNamespace(storage_bucket='vision-board-bot-images',
        storage_endpoint_url='https://storage.yandexcloud.net',storage_public_base_url='https://cdn.example')
    assets.publish=AsyncMock(side_effect=lambda path: assets.canonical_image_url(assets.object_key(path)))
    return assets


async def done_image(studio,assets,*,color='navy',legacy_kind=False):
    project,settings,_=studio
    character=(await repo.children('character',project))[0]
    job=await repo.quote('character',character['id'],'image',settings)
    asset=await assets.retain(json.loads(job['snapshot']),'image',data=png(color),job_id=job['id'])
    await repo.update_job(job['id'],status='done',actual=27000,notified=1)
    if legacy_kind:
        async with repo.connect() as db:
            await db.execute("UPDATE cartoon_jobs SET kind='character_reference' WHERE id=?",(job['id'],))
            await db.execute("UPDATE cartoon_assets SET kind='character_reference' WHERE id=?",(asset,))
            await db.commit()
    return await repo.job(job['id']),asset


@pytest.mark.parametrize('legacy_kind',[False,True])
async def test_historical_done_job_card_backfills_link_and_exposes_reference_actions(studio,result_assets,legacy_kind):
    project,settings,_=studio
    job,asset=await done_image(studio,result_assets,legacy_kind=legacy_kind)
    pressed=event('studio:job:'+job['id'])
    await dispatch_callback(create_router(456,settings,result_assets),pressed,await ready_context())
    saved=await repo.job(job['id'])
    assert saved['result_url']==PREFIX+'studio/'+job['id']+'/image.png'
    assert saved['actual']==27000 and saved['status']=='done' and saved['notified']==1
    sent=pressed.bot.send_message.await_args.kwargs
    assert saved['result_url'] in sent['text']
    buttons=[b for row in sent['attachments'][0].payload.buttons for b in row]
    assert ('Показать картинку','studio:show_image:'+job['id']) in [(b.text,b.payload) for b in buttons if hasattr(b,'payload')]
    assert '✅ Утвердить эталон' in [b.text for b in buttons]
    assert '🔄 Перегенерировать' in [b.text for b in buttons]
    assert any(getattr(b,'url',None)==saved['result_url'] for b in buttons)
    assert (await repo.job_result(job['id']))['id']==asset
    assert not any(j['status']=='queued' for j in await repo.job_list(project))
    again=event('studio:job:'+job['id'])
    await dispatch_callback(create_router(456,settings,result_assets),again,await ready_context())
    result_assets.publish.assert_awaited_once()


async def test_show_picture_sends_one_image_attachment_from_yandex_url(studio,result_assets,monkeypatch):
    _,settings,_=studio
    job,_=await done_image(studio,result_assets)
    attachment=AttachmentUpload(type='image',payload={'token':'max-result-image'})
    upload=AsyncMock(return_value=attachment)
    monkeypatch.setattr('image_service.upload_image_for_max',upload)
    pressed=event('studio:show_image:'+job['id'])
    await dispatch_callback(create_router(456,settings,result_assets),pressed,await ready_context())
    url=(await repo.job(job['id']))['result_url']
    upload.assert_awaited_once_with(pressed.bot,url)
    assert url.startswith(PREFIX)
    assert pressed.bot.send_message.await_args.kwargs['attachments']==[attachment]
    assert pressed.bot.send_message.await_args.kwargs['user_id']==456


@pytest.mark.parametrize('legacy_kind',[False,True])
async def test_old_job_uses_its_own_image_and_approval_pins_exact_reference(studio,result_assets,legacy_kind):
    _,settings,_=studio
    old,old_asset=await done_image(studio,result_assets,color='red',legacy_kind=legacy_kind)
    new,new_asset=await done_image(studio,result_assets,color='blue')
    assert old_asset!=new_asset
    await dispatch_callback(create_router(456,settings,result_assets),event('studio:job:'+old['id']),await ready_context())
    assert (await repo.job(old['id']))['result_url'].endswith((await repo.job_result(old['id']))['path'])
    approve=event('studio:approve_reference:'+old['id'])
    await dispatch_callback(create_router(456,settings,result_assets),approve,await ready_context())
    snap=await repo.snapshot('character',old['target_id'])
    assert snap['spec']['reference']==old_asset and snap['assets']['image']['approved']==1
    assert snap['assets']['image']['id']!=new_asset
    revision=snap['revision']
    await dispatch_callback(create_router(456,settings,result_assets),event('studio:approve_reference:'+old['id']),await ready_context())
    assert (await repo.get('character',old['target_id']))['revision']==revision


async def test_regenerate_button_opens_quote_without_any_queued_generation(studio,result_assets):
    project,settings,_=studio
    job,_=await done_image(studio,result_assets)
    card=event('studio:job:'+job['id'])
    router=create_router(456,settings,result_assets)
    await dispatch_callback(router,card,await ready_context())
    buttons=[b for row in card.bot.send_message.await_args.kwargs['attachments'][0].payload.buttons for b in row]
    regenerate=next(b for b in buttons if b.text=='🔄 Перегенерировать')
    pressed=event(regenerate.payload)
    await dispatch_callback(router,pressed,await ready_context())
    assert 'Подтверждение задания' in pressed.bot.send_message.await_args.kwargs['text']
    assert any(j['status']=='quoted' for j in await repo.job_list(project))
    assert await repo.claim() is None


@pytest.mark.parametrize('action',['job','show_image','approve_reference','publish_result'])
async def test_result_actions_do_not_expose_another_admin_project(studio,result_assets,action):
    _,settings,_=studio
    job,_=await done_image(studio,result_assets)
    pressed=event('studio:'+action+':'+job['id'],actor=999)
    await dispatch_callback(create_router(999,settings,result_assets),pressed,await ready_context())
    assert 'доступ' in pressed.bot.send_message.await_args.kwargs['text'].lower()
    result_assets.publish.assert_not_awaited()


async def test_missing_asset_does_not_substitute_latest_character_image(studio,result_assets):
    project,settings,_=studio
    job,_=await done_image(studio,result_assets)
    async with repo.connect() as db:
        await db.execute('DELETE FROM cartoon_assets WHERE job_id=?',(job['id'],)); await db.commit()
    await done_image(studio,result_assets,color='blue')
    pressed=event('studio:job:'+job['id'])
    await dispatch_callback(create_router(456,settings,result_assets),pressed,await ready_context())
    assert 'не найден' in pressed.bot.send_message.await_args.kwargs['text']
    assert not any(j['status']=='queued' for j in await repo.job_list(project))


async def test_publication_retry_after_restart_never_repeats_paid_image_request(studio,result_assets,caplog):
    project,settings,_=studio
    char=(await repo.children('character',project))[0]
    job=await repo.quote('character',char['id'],'image',settings)
    await repo.budget(project,1000000); await repo.confirm(job['id'],456)
    providers=SimpleNamespace(preflight=lambda kind:None,image=AsyncMock(return_value=png()))
    attempts=[]
    async def publish(path):
        attempts.append(path)
        if len(attempts)==1: raise RuntimeError('S3 down')
        return result_assets.canonical_image_url(result_assets.object_key(path))
    result_assets.publish=AsyncMock(side_effect=publish)
    worker=Worker(SimpleNamespace(),456,settings,providers,result_assets)
    await worker.execute(await repo.claim())
    assert (await repo.job(job['id']))['status']=='publishing'
    assert await repo.job_result(job['id'])
    await repo.init()
    await repo.update_job(job['id'],next_attempt=0,actual=27000)
    await worker.execute(await repo.claim())
    saved=await repo.job(job['id'])
    assert saved['status']=='done' and saved['result_url']==PREFIX+'studio/'+job['id']+'/image.png'
    assert saved['actual']==27000
    providers.image.assert_awaited_once()


async def test_cached_foreign_url_is_republished_to_yandex_not_used_for_max(studio,result_assets):
    job,asset_id=await done_image(studio,result_assets)
    async with repo.connect() as db:
        await db.execute('UPDATE cartoon_assets SET public_url=? WHERE id=?',('https://cdn.example/not-yandex.png',asset_id))
        await db.commit()
    _,url=await result_assets.result_for_job(job)
    assert url.startswith(PREFIX)
    result_assets.publish.assert_awaited_once()


async def test_publish_image_constructs_canonical_yandex_bucket_url_even_with_cdn_base(studio,monkeypatch):
    assets=studio[2]
    assets.config=SimpleNamespace(storage_bucket='vision-board-bot-images',storage_endpoint_url='https://storage.yandexcloud.net',
                                 storage_public_base_url='https://cdn.example')
    client=SimpleNamespace(put_object=Mock())
    monkeypatch.setattr('image_service._storage_client',lambda config:client)
    path,_=await assets.write(png(),'png')
    assert await assets.publish(path)==PREFIX+'multstudio/'+path
    assert client.put_object.call_args.kwargs['Bucket']=='vision-board-bot-images'
    assert client.put_object.call_args.kwargs['Body']==png()


async def test_init_adds_missing_result_url_column_idempotently(studio):
    async with repo.connect() as db:
        await db.execute('ALTER TABLE cartoon_jobs DROP COLUMN result_url'); await db.commit()
    await repo.init(); await repo.init()
    async with repo.connect() as db:
        columns=await repo.rows(db,'PRAGMA table_info(cartoon_jobs)')
    assert [c['name'] for c in columns].count('result_url')==1


@pytest.mark.parametrize('max_fails',[False,True])
async def test_new_image_path_put_object_url_and_immediate_max_delivery_agree(studio,monkeypatch,max_fails):
    project,settings,assets=studio
    assets.config=SimpleNamespace(storage_bucket='vision-board-bot-images',storage_endpoint_url='https://storage.yandexcloud.net',
                                 storage_public_base_url=PREFIX.rstrip('/'))
    s3=SimpleNamespace(put_object=Mock())
    monkeypatch.setattr('image_service._storage_client',lambda config:s3)
    image=AttachmentUpload(type='image',payload={'token':'generated-reference'})
    uploader=AsyncMock(return_value=image)
    monkeypatch.setattr('image_service.upload_image_for_max',uploader)
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError('MAX temporary'),object()] if max_fails else None,
                                              return_value=object()))
    char=(await repo.children('character',project))[0]
    item=await repo.quote('character',char['id'],'image',settings)
    await repo.budget(project,1000000); await repo.confirm(item['id'],456)
    providers=SimpleNamespace(preflight=lambda kind:None,image=AsyncMock(return_value=png()))
    worker=Worker(bot,456,settings,providers,assets)
    await worker.execute(await repo.claim())
    saved=await repo.job(item['id']); asset=await repo.job_result(item['id'])
    relative=item['id']+'/image.png'; key='studio/'+relative
    assert asset['path']==relative and assets.path(relative).read_bytes()==png()
    assert saved['result_url']==PREFIX+key and saved['status']=='done'
    assert s3.put_object.call_args.kwargs=={'Bucket':'vision-board-bot-images','Key':key,'Body':png(),'ContentType':'image/png'}
    if max_fails:
        assert saved['notified']==0
        await worker.notices()
    message=bot.send_message.await_args.kwargs
    assert message['user_id']==456 and message['text']=='✅ Эталон сгенерирован!'
    assert message['attachments'][0]==image
    assert len([a for a in message['attachments'] if a.type=='inline_keyboard'])==1
    buttons=[b for row in message['attachments'][1].payload.buttons for b in row]
    assert [b.payload for b in buttons]==['studio:approve_reference:'+item['id'],
                                        'studio:make:image:character:'+char['id']]
    assert (await repo.job(item['id']))['notified']==1
    await worker.notices()
    assert bot.send_message.await_count==(2 if max_fails else 1)
    providers.image.assert_awaited_once()
    s3.put_object.assert_called_once()


async def test_wrong_db_path_is_recovered_by_hash_to_job_folder_and_really_uploaded(studio,result_assets,monkeypatch):
    job,asset_id=await done_image(studio,result_assets)
    original=(await repo.job_result(job['id']))['path']
    legacy='f3689c2ecb2c4f978831b21b039c79ae.png'
    result_assets.path(original).rename(result_assets.path(legacy))
    missing='studio/'+job['id']+'/77e39-missing.png'
    async with repo.connect() as db:
        await db.execute('UPDATE cartoon_assets SET path=?,public_url=? WHERE id=?',(missing,PREFIX+missing,asset_id))
        await db.execute('UPDATE cartoon_jobs SET result_url=? WHERE id=?',(PREFIX+missing,job['id']))
        await db.commit()
    s3=SimpleNamespace(put_object=Mock())
    monkeypatch.setattr('image_service._storage_client',lambda config:s3)
    result_assets.publish=Assets.publish.__get__(result_assets,Assets)
    asset,url=await result_assets.result_for_job(await repo.job(job['id']))
    assert asset['path']==job['id']+'/image.png'
    assert url==PREFIX+'studio/'+job['id']+'/image.png'
    assert result_assets.path(legacy).exists()  # Original never deleted.
    assert s3.put_object.call_args.kwargs['Body']==png()
    assert s3.put_object.call_args.kwargs['Key']=='studio/'+job['id']+'/image.png'
    assert (await repo.job(job['id']))['actual']==27000
    assert missing in json.loads(asset['metadata'])['previous_paths']


async def test_legacy_repair_never_guesses_image_without_matching_hash(studio,result_assets):
    job,asset_id=await done_image(studio,result_assets)
    original=(await repo.job_result(job['id']))['path']
    result_assets.path(original).unlink()
    await result_assets.write(png('red'),'png')
    with pytest.raises(ValueError,match='SHA-256'):
        await result_assets.result_for_job(job)
    result_assets.publish.assert_not_awaited()
    assert (await repo.job(job['id']))['status']=='done'


async def test_canonical_file_cannot_be_overwritten_by_different_result_for_same_job(studio):
    assets=studio[2]
    key='a'*32
    relative,_=await assets.write(png(),'png',job_id=key,name='image.png')
    with pytest.raises(ValueError,match='другим содержимым'):
        await assets.write(png('red'),'png',job_id=key,name='image.png')
    assert assets.path(relative).read_bytes()==png()
    assert not list(assets.path(key).glob('.part-*'))


async def test_repairing_reference_paths_does_not_stale_an_existing_quote(studio,result_assets):
    project,settings,_=studio
    job,asset_id=await done_image(studio,result_assets)
    await repo.approve_character_result(job['id'],456)
    original=(await repo.job_result(job['id']))['path']
    result_assets.path(original).rename(result_assets.path('legacy.png'))
    async with repo.connect() as db:
        await db.execute('UPDATE cartoon_assets SET path=? WHERE id=?',('studio/obsolete.png',asset_id)); await db.commit()
    quote=await repo.quote('character',job['target_id'],'image',settings)
    await result_assets.result_for_job(job)
    await repo.budget(project,2000000)
    assert (await repo.confirm(quote['id'],456))['status']=='queued'

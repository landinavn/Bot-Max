"""Rebuild completed greetings from saved inputs without another paid request."""
from unittest.mock import AsyncMock
import json

import pytest

import ded_moroz as dm
from ded_moroz_rebuild import queue_rebuild
from test_ded_moroz import setup_dm, audio_order, step


async def completed(worker):
    row=await audio_order(worker);key=row['id']
    for name,data in [('heygen.mp4',b'original HeyGen video'),('approved.mp3',b'approved voice'),('greeting.mp4',b'old montage')]:
        await worker.assets.write(data,name.split('.')[-1],job_id=key,name=name)
    await dm.update(key,status='done',video_id='saved_video',raw_path=key+'/heygen.mp4',
        approved_audio_path=key+'/approved.mp3',audio_path=key+'/unused.mp3',
        output_path=key+'/greeting.mp4',output_url='https://storage.example/old.mp4',notified=1)
    return await dm.get(key)


@pytest.mark.parametrize('form',[None,'Мишеньки'])
async def test_rebuild_uses_saved_video_and_approved_audio_and_delivers_new_mp4(credit_db,setup_dm,monkeypatch,form):
    import video_service
    _,_,worker=setup_dm;row=await completed(worker);key=row['id']
    old=worker.assets.path(row['output_path'])
    result=queue_rebuild(worker.config,worker.settings,name_genitive=form)
    saved=await dm.get(key)
    assert result['order_id']==key and result['paid_requests']==0 and saved['status']=='render'
    assert saved['output_path']!=row['output_path'] and old.read_bytes()==b'old montage'
    assert saved['video_id']==row['video_id'] and saved['notified']==1
    assert list(old.parent.glob('rebuild-*.json'))
    async def render(video,photo,speech,examples,target,name,**options):
        assert options==({"name_genitive":form} if form else {})
        assert video==worker.assets.path(row['raw_path'])
        assert speech==worker.assets.path(row['approved_audio_path'])
        target.write_bytes(b'new montage without overlay');return target
    render_mock=AsyncMock(side_effect=render)
    monkeypatch.setattr(video_service,'render_ded_moroz',render_mock)
    with pytest.raises(ValueError):queue_rebuild(worker.config,worker.settings)
    for _ in range(3):await step(worker,key)
    saved=await dm.get(key)
    assert saved['status']=='done' and saved['output_path']==result['target']
    assert worker.assets.path(saved['output_path']).read_bytes()==b'new montage without overlay'
    assert old.read_bytes()==b'old montage'
    worker.tts.generate_speech.assert_not_awaited()
    worker.heygen.create_video.assert_not_awaited()
    worker.heygen.video_status.assert_not_awaited()
    assert worker.bot.upload_media.await_args.args[0].type.value=='video'
    assert (await credit_db.get_all_counters()).get('dm_video_created',0)==0


async def test_missing_source_refuses_rebuild_without_modifying_completed_order(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await completed(worker)
    worker.assets.path(row['raw_path']).unlink()
    with pytest.raises(ValueError,match='видео HeyGen'):
        queue_rebuild(worker.config,worker.settings,row['id'])
    assert await dm.get(row['id'])==row
    worker.tts.generate_speech.assert_not_awaited()


async def test_another_active_order_is_not_overwritten_or_skipped(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await completed(worker)
    new=await dm.begin(456)
    with pytest.raises(ValueError,match='завершённый'):
        queue_rebuild(worker.config,worker.settings)
    with pytest.raises(ValueError,match='другой незавершённый'):
        queue_rebuild(worker.config,worker.settings,row['id'])
    assert (await dm.get(row['id']))['status']=='done'
    assert (await dm.get(new['id']))['status']=='input'

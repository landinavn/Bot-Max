import array
import hashlib
import math
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from multstudio import repository as repo
from multstudio.render import Renderer, process, probe, duration
from multstudio.router import create_router
from test_multstudio import studio, ready_scene
from test_multstudio_project_menu import event
from test_handlers import dispatch_callback, ready_context


async def tone(path, seconds, frequency=440, offset=0, rate=48000):
    await process('ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                  f'sine=frequency={frequency}:sample_rate={rate}:duration={seconds}',
                  '-c:a','aac','-output_ts_offset',str(offset),path)


async def clip(path, seconds, color='navy', offset=0, frequency=None):
    args=['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',f'color=c={color}:s=64x96:r=30:d={seconds}']
    if frequency:
        args+=['-f','lavfi','-i',f'sine=frequency={frequency}:sample_rate=48000:duration={seconds}',
               '-c:a','aac']
    await process(*args,'-c:v','libx264','-threads','1','-pix_fmt','yuv420p',
                  '-output_ts_offset',str(offset),path)


async def samples(path):
    raw=await process('ffmpeg','-nostdin','-v','error','-i',path,'-map','0:a:0',
                      '-ac','1','-ar','48000','-f','f32le','-')
    data=array.array('f'); data.frombytes(raw)
    return data


def rms(data,start,end):
    data=data[round(start*48000):round(end*48000)]
    return math.sqrt(sum(x*x for x in data)/max(1,len(data)))


def amplitude(data,start,end,frequency):
    values=data[round(start*48000):round(end*48000)]
    omega=2*math.pi*frequency/48000
    real=sum(value*math.cos(omega*i) for i,value in enumerate(values))
    imag=sum(value*math.sin(omega*i) for i,value in enumerate(values))
    return 2*math.hypot(real,imag)/len(values)


def scene(video,voice,seconds,offset=0):
    return dict(spec=dict(duration=seconds,dialogue=[]),editing={'speech_start':offset},
                assets=dict(video=dict(kind='video',path=video),speech=dict(path=voice)))


async def test_real_nonzero_input_pts_are_rebased_without_leading_silence(studio):
    _,_,assets=studio; assets.root.mkdir(parents=True,exist_ok=True)
    await clip(assets.path('v.mp4'),0.4,offset=2)
    await tone(assets.path('a.m4a'),0.9,offset=1.5)
    assert float((await probe(assets.path('v.mp4')))['streams'][0]['start_time'])>1
    assert await duration(assets.path('v.mp4'))==pytest.approx(0.4,abs=0.04)
    assert await duration(assets.path('a.m4a'))==pytest.approx(0.9,abs=0.04)
    result=await Renderer(assets).scene(scene('v.mp4','a.m4a',0.4))
    info=await probe(result)
    assert all(abs(float(s['start_time']))<0.001 for s in info['streams'])
    assert all(float(s['duration'])>=1.05 for s in info['streams'])
    data=await samples(result)
    assert rms(data,0.03,0.15)>0.04
    assert rms(data,0.75,0.85)>0.04  # The end of the spoken line survives.
    assert float(info['format']['duration'])<1.25  # No timestamp-sized padding.


async def test_short_voice_pads_silence_and_preserves_full_video(studio):
    _,_,assets=studio; assets.root.mkdir(parents=True,exist_ok=True)
    await clip(assets.path('long.mp4'),1.2)
    await tone(assets.path('short.m4a'),0.3)
    result=await Renderer(assets).scene(scene('long.mp4','short.m4a',0.3))
    info=await probe(result)
    video=next(s for s in info['streams'] if s['codec_type']=='video')
    assert float(video['duration'])==pytest.approx(1.2,abs=1/30)
    assert int(video['nb_frames'])>=36
    data=await samples(result)
    assert rms(data,0.05,0.2)>0.04
    assert rms(data,0.8,1.1)<0.001


async def test_three_second_intro_delays_voice_and_subtitles_but_keeps_whole_line(studio):
    _,_,assets=studio; assets.root.mkdir(parents=True,exist_ok=True)
    await clip(assets.path('first.mp4'),0.4)
    await tone(assets.path('first.m4a'),0.6)
    shot=scene('first.mp4','first.m4a',0.4,offset=3)
    shot['spec']['dialogue']=[{'text':'Что?! Опять скоро Новый год?!'}]
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in assets.root.iterdir()}
    result=await Renderer(assets).scene(shot)
    info=await probe(result)
    video=next(s for s in info['streams'] if s['codec_type']=='video')
    assert float(video['duration'])>=3.8-1/30
    assert int(video['nb_frames'])>=114
    data=await samples(result)
    assert rms(data,0.1,2.95)<0.001
    assert rms(data,3.04,3.2)>0.04 and rms(data,3.4,3.5)>0.04
    srt=next(assets.root.glob('*.srt')).read_text()
    assert '00:00:03,000 --> 00:00:03,' in srt
    for name,checksum in before.items():
        assert hashlib.sha256(assets.path(name).read_bytes()).hexdigest()==checksum


async def test_concatenated_tts_lines_have_no_source_pts_gaps_or_speed_change(studio):
    _,_,assets=studio; assets.root.mkdir(parents=True,exist_ok=True)
    await tone(assets.path('line1.m4a'),0.3,frequency=440,offset=1.5,rate=22050)
    await tone(assets.path('line2.m4a'),0.4,frequency=660,offset=2,rate=44100)
    result=await Renderer(assets).speech([assets.path('line1.m4a'),assets.path('line2.m4a')])
    data=await samples(result)
    assert 0.69<len(data)/48000<0.85
    assert amplitude(data,0.05,0.2,440)>0.06
    assert amplitude(data,0.45,0.6,660)>0.06


@pytest.mark.parametrize('music',[False,True])
async def test_three_scene_boundaries_do_not_accumulate_aac_delay_or_duck_speech(studio,music):
    _,_,assets=studio; assets.root.mkdir(parents=True,exist_ok=True)
    lengths=[0.3,0.4,0.5]
    colors=['red','green','blue']
    frequencies=[440,660,880]
    paths=[]
    shots=[]
    for i,(seconds,color,frequency) in enumerate(zip(lengths,colors,frequencies)):
        path=assets.path(f'clip{i}.mp4')
        await clip(path,seconds,color=color,frequency=frequency)
        paths.append(path)
        shots.append(dict(spec=dict(duration=seconds,dialogue=[]),assets={}))
    renderer=Renderer(assets)
    renderer.scene=AsyncMock(side_effect=paths)
    snap=dict(spec={},scenes=shots,assets={})
    if music:
        await tone(assets.path('music.m4a'),0.2,frequency=110,offset=1.2)
        snap['assets']['music']={'path':'music.m4a'}
    result=await renderer.assemble(snap)
    info=await probe(result)
    assert all(abs(float(s['start_time']))<0.001 for s in info['streams'])
    assert all(float(s['duration'])==pytest.approx(1.2,abs=0.025) for s in info['streams'])
    data=await samples(result)
    cursor=0
    for seconds,frequency,color in zip(lengths,frequencies,colors):
        assert amplitude(data,cursor+0.06,cursor+0.2,frequency)>0.07
        pixel=await process('ffmpeg','-nostdin','-v','error','-ss',str(cursor+0.1),'-i',result,
                            '-frames:v','1','-vf','scale=1:1','-f','rawvideo','-pix_fmt','rgb24','-')
        assert pixel[colors.index(color)]>max(pixel[j] for j in range(3) if j!=colors.index(color))+40
        cursor+=seconds
    background=amplitude(data,0.07,0.2,110)
    assert 0.004<background<0.025 if music else background<0.003
    # A fresh call always creates another local result from the same saved clips.
    renderer.scene=AsyncMock(side_effect=paths)
    rebuilt=await renderer.assemble(snap)
    assert rebuilt!=result and rebuilt.exists() and all(p.exists() for p in paths)


async def test_editing_defaults_and_changes_preserve_all_scene_contexts(studio):
    project,_,_=studio
    _,episode=await ready_scene(studio)
    rows=await repo.children('scene',episode['id'])
    before=[await repo.snapshot('scene',s['id']) for s in rows]
    assert (await repo.scene_editing(rows[0]['id']))['speech_start']==0
    for s in rows[1:]:
        assert (await repo.scene_editing(s['id']))['speech_start']==(1 if s['number']==5 else 0)
    original=await repo.snapshot('episode',episode['id'])
    await repo.set_scene_speech_start(rows[0]['id'],456,'1,5')
    await repo.init()
    after=await repo.snapshot('episode',episode['id'])
    assert after['scenes'][0]['editing']['speech_start']==1.5
    assert original['context_hash']!=after['context_hash']
    assert before==[await repo.snapshot('scene',s['id']) for s in rows]
    with pytest.raises(ValueError,match='доступа'):
        await repo.set_scene_speech_start(rows[0]['id'],999,2)
    assert (await repo.scene_editing(rows[0]['id']))['speech_start']==1.5
    assert await repo.claim() is None


@pytest.mark.parametrize('value',[True,-1,31,'bad','nan','inf',None])
def test_invalid_editing_start_is_rejected(value):
    with pytest.raises(ValueError): repo.speech_start(value)


async def test_scene_menu_has_independent_speech_start_editor(studio):
    _,settings,assets=studio
    shot,_=await ready_scene(studio)
    pressed=event('studio:s:'+shot['id'])
    router=create_router(456,settings,assets)
    await dispatch_callback(router,pressed,await ready_context())
    assert 'Начало речи: 0 с' in pressed.bot.send_message.await_args.kwargs['text']
    pressed=event('studio:speech_start:'+shot['id'])
    context=await ready_context()
    await dispatch_callback(router,pressed,context)
    assert '0 секунде' in pressed.bot.send_message.await_args.kwargs['text']
    assert (await context.get_data())['studio_input']['action']=='speech_start'
    await dispatch_callback(router,event('studio:home'),context)
    assert await context.get_state() is None
    assert not (await context.get_data()).get('studio_input')
    assert await repo.claim() is None


def test_final_card_large_text_wraps_inside_safe_area(tmp_path):
    path=tmp_path/'title.png'
    Renderer.endcard(['Маленькие чудеса Ани','А о чём мечтаете вы?','Продолжение следует…'],path)
    with Image.open(path) as im:
        box=im.getchannel('A').getbbox()
        assert im.size==(1080,1920)
        assert 40<=box[0]<box[2]<=1040 and 100<=box[1]<box[3]<=1740
        # Each rendered text line now has roughly subtitle-sized glyphs, not the old 42px title.
        occupied=[y for y in range(im.height) if im.getchannel('A').crop((0,y,1080,y+1)).getbbox()]
        groups=[]
        for y in occupied:
            if not groups or y>groups[-1][-1]+1: groups.append([y])
            else: groups[-1].append(y)
        assert max(map(len,groups))>60


async def test_new_assembly_quote_uses_saved_media_and_never_calls_paid_providers(studio,monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from multstudio.worker import Worker
    project,settings,assets=studio
    _,episode=await ready_scene(studio)
    # Make all previously produced media available; quoting/rendering uses no generator.
    for shot in await repo.children('scene',episode['id']):
        snap=await repo.snapshot('scene',shot['id'])
        for kind,suffix in [('image','png'),('video','mp4'),('speech','mp3')]:
            await assets.retain(snap,kind,data=(kind+shot['id']).encode(),suffix=suffix)
    first=(await repo.children('scene',episode['id']))[0]
    initial=await repo.quote('episode',episode['id'],'assemble',settings)
    await repo.set_scene_speech_start(first['id'],456,3.5)
    revised=await repo.quote('episode',episode['id'],'assemble',settings)
    assert revised['id']!=initial['id'] and revised['estimate']==0
    import json
    snap=json.loads(revised['snapshot'])
    assert snap['quote']['render_version']==3
    assert snap['scenes'][0]['editing']['speech_start']==3.5
    assert all(s['assets'].get('video') and s['assets'].get('speech') for s in snap['scenes'])
    # Confirming a stale free quote must not silently use previous timing.
    with pytest.raises(ValueError,match='устарела'):
        await repo.confirm(initial['id'],456)
    await repo.confirm(revised['id'],456)
    item=await repo.claim()
    providers=SimpleNamespace(preflight=Mock(),image=AsyncMock(),speech_line=AsyncMock(),kling=Mock())
    worker=Worker(SimpleNamespace(),456,settings,providers,assets)
    worker.renderer.assemble=AsyncMock(return_value=assets.path('assembled.mp4'))
    assets.path('assembled.mp4').write_bytes(b'already rendered offline')
    worker.save_result=AsyncMock()
    await worker.execute(item)
    worker.renderer.assemble.assert_awaited_once()
    worker.save_result.assert_awaited_once()
    providers.preflight.assert_not_called()
    providers.image.assert_not_awaited()
    providers.speech_line.assert_not_awaited()

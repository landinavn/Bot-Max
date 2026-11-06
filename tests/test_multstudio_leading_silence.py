import array
import hashlib
import math
import wave

import pytest

from multstudio import repository as repo
from multstudio.render import Renderer, duration, process
from test_multstudio import studio, ready_scene
from test_multstudio_av_sync import clip, samples, rms, scene


def speech_file(path,lead=3.24335,*,only_silence=False):
    data=array.array('h',[0])*round(lead*48000)
    if not only_silence:
        for frequency,seconds in [(440,0.4),(0,0.6),(660,0.4),(0,0.2)]:
            data.extend(round(8000*math.sin(2*math.pi*frequency*i/48000)) if frequency else 0
                        for i in range(round(seconds*48000)))
    with wave.open(str(path),'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(48000)
        output.writeframes(data.tobytes())


@pytest.mark.parametrize('offset',[0,1])
@pytest.mark.parametrize('codec',['wav','mp3'])
async def test_real_initial_silence_is_removed_before_configured_delay_and_internal_pause_is_kept(studio,offset,codec):
    _,_,assets=studio; assets.root.mkdir(parents=True,exist_ok=True)
    await clip(assets.path('scene.mp4'),0.5)
    path=assets.path('speech.wav'); speech_file(path)
    if codec=='mp3':
        path=assets.path('speech.mp3')
        await process('ffmpeg','-nostdin','-v','error','-y','-i',str(assets.path('speech.wav')),
                      '-c:a','libmp3lame','-b:a','64k','-ar','44100',str(path))
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    shot=scene('scene.mp4',path.name,0.5,offset=offset)
    shot['spec']['dialogue']=[{'text':'Маме, папе, детям…'}]
    result=await Renderer(assets).scene(shot)
    audio=await samples(result)
    if offset:
        assert rms(audio,0.05,0.98)<0.001
    assert rms(audio,offset+0.05,offset+0.25)>0.06
    assert rms(audio,offset+0.55,offset+0.9)<0.001  # Interior 0.6-second pause remains.
    assert rms(audio,offset+1.1,offset+1.3)>0.06  # Last spoken word is not cut.
    assert offset+1.75<await duration(result)<offset+1.95
    text=next(assets.root.glob('*.srt')).read_text()
    assert f'00:00:0{offset},000 --> 00:00:0{offset+1},' in text
    assert hashlib.sha256(path.read_bytes()).hexdigest()==checksum


@pytest.mark.parametrize('lead',[0,0.1])
async def test_no_long_initial_silence_means_no_change_to_audio_timing(studio,lead):
    _,_,assets=studio; assets.root.mkdir(parents=True,exist_ok=True)
    path=assets.path('normal.wav'); speech_file(path,lead=lead)
    trim,seconds=await Renderer(assets).speech_timing(path)
    assert trim==0 and seconds==pytest.approx(1.6+lead,abs=0.001)


async def test_fully_silent_saved_audio_has_clear_error_without_modification(studio):
    _,_,assets=studio; assets.root.mkdir(parents=True,exist_ok=True)
    path=assets.path('empty.wav'); speech_file(path,lead=1,only_silence=True)
    before=path.read_bytes()
    with pytest.raises(ValueError,match='только тишину'):
        await Renderer(assets).speech_timing(path)
    assert path.read_bytes()==before


async def test_episode_subtitles_use_trimmed_voice_duration_and_assets_keep_their_identity(studio):
    project,_,assets=studio
    _,episode=await ready_scene(studio)
    row=(await repo.children('scene',episode['id']))[3]
    saved=await repo.snapshot('scene',row['id'])
    assets.root.mkdir(parents=True,exist_ok=True)
    await clip(assets.path('four.mp4'),0.5)
    speech_file(assets.path('four.wav'))
    shot=dict(saved,spec=dict(saved['spec'],duration=0.5),
              assets=dict(video={'kind':'video','path':'four.mp4'},speech={'path':'four.wav'}),
              editing={'speech_start':0,'video_start':0})
    snap=dict(project_id=project,target='episode',id=episode['id'],context_hash='test-timeline',
              spec={},assets={},scenes=[shot])
    result=await Renderer(assets).assemble(snap)
    async with repo.connect() as db:
        sub=await repo.one(db,"SELECT * FROM cartoon_assets WHERE target='episode' AND target_id=? AND kind='subtitle' ORDER BY rowid DESC LIMIT 1",(episode['id'],))
    assert '00:00:00,000 --> 00:00:01,' in assets.path(sub['path']).read_text()
    assert rms(await samples(result),0.05,0.25)>0.06
    assert saved==await repo.snapshot('scene',row['id'])
    assert await repo.claim() is None

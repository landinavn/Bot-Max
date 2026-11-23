"""Sung-word alignment trims instrumental audio, rather than confusing it with silence."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import song_alignment as align
import media_service as media


def test_matches_approved_first_words_including_yo_and_keeps_consonant():
    _,expected=align.first_lyric('[Verse 1]\nВ окне родного дома свет не гаснет,\n[Chorus]\nМама')
    transcript=[{'word':w,'start':i+.06} for i,w in enumerate(['Музыка','В','окне','родного','дома'])]
    assert align.matched_start(transcript,expected)==1.
    with pytest.raises(ValueError):align.matched_start([{'word':'Неправильная строка','start':0}],expected)
    with pytest.raises(ValueError):align.matched_start([{'word':'В окне родного дома','start':float('nan')}],expected)


@respx.mock
async def test_transcribes_only_sample_and_requests_word_timestamps(tmp_path,monkeypatch):
    audio=tmp_path/'full.mp3';audio.write_bytes(b'audio')
    async def run(*args,**kwargs):
        assert '-t' in args and args[args.index('-t')+1]=='30'
        (tmp_path/'vocal-start-sample.mp3').write_bytes(b'sample')
    monkeypatch.setattr(media,'run_media_process',run)
    monkeypatch.setattr(align.Config,'from_env',lambda:SimpleNamespace(image_api_key='test-key',openai_proxy_url=None))
    route=respx.post('https://api.openai.com/v1/audio/transcriptions').mock(return_value=httpx.Response(200,json={
        'language':'russian','duration':30,'text':'В окне родного дома','words':[
            {'word':w,'start':4.06+i*.3,'end':4.3+i*.3} for i,w in enumerate(['В','окне','родного','дома'])]}))
    assert await align.detect_song_start(audio,'[Verse 1]\nВ окне родного дома свет не гаснет',tmp_path)==pytest.approx(4.)
    raw=route.calls[0].request.content
    assert b'whisper-1' in raw and b'verbose_json' in raw and b'word' in raw
    assert b'full.mp3' not in raw and b'vocal-start-sample.mp3' in raw


async def test_render_removes_real_non_silent_music_intro_and_discards_native_clip_audio(tmp_path):
    import mother_story
    from array import array
    import math
    clip=tmp_path/'scene.mp4';song=tmp_path/'song.wav';out=tmp_path/'out.mp4'
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=160x90:r=25:d=10',
        '-f','lavfi','-i','sine=frequency=900:duration=10','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(clip)],check=True)
    # Intro is audible (220 Hz), so silencedetect alone could not remove it. The vocal stand-in is 660 Hz.
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=220:duration=2',
        '-f','lavfi','-i','sine=frequency=660:duration=61','-filter_complex','[0:a][1:a]concat=n=2:v=0:a=1[a]',
        '-map','[a]',str(song)],check=True)
    await mother_story.render_local([clip]*6,song,out,60,audio_start=2.)
    raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(out),'-t','0.5','-ac','1','-ar','16000','-f','f32le','-'])
    samples=array('f');samples.frombytes(raw)
    power={f:abs(sum(v*complex(math.cos(2*math.pi*f*i/16000),math.sin(2*math.pi*f*i/16000))
        for i,v in enumerate(samples))) for f in (220,660,900)}
    assert max(power,key=power.get)==660  # neither intro's 220 Hz nor native video's 900 Hz
    p=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(out)]))
    assert abs(float(p['format']['duration'])-60)<.15

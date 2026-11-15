"""Real offline FFmpeg: preserve HeyGen background, timed rectangular PiP and AAC."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from video_service import render_ded_moroz
from ded_moroz_intro import INTRO_SECONDS,recipient_line


def ff(*args):
    subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*map(str,args)],check=True,capture_output=True)


@pytest.mark.parametrize('with_fire',[True,False])
@pytest.mark.parametrize('photo_size,border_pixel',[((500,500),(1426,800)),((941,1672),(1426,800))])
async def test_real_landscape_render_overlay_pip_audio_and_safe_name(tmp_path,photo_size,border_pixel,with_fire,caplog):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):pytest.skip('FFmpeg required')
    examples=tmp_path/'examples';(examples/'audio').mkdir(parents=True)
    video=tmp_path/'heygen.mp4';child=tmp_path/'child.png';speech=tmp_path/'voice.mp3';out=tmp_path/'result.mp4'
    ff('-f','lavfi','-i','color=c=black:s=320x180:r=30:d=1.5','-c:v','libx264','-pix_fmt','yuv420p',video)
    if with_fire:
        ff('-f','lavfi','-i','color=c=red:s=320x180:r=30:d=0.5','-c:v','libx264','-pix_fmt','yuv420p',examples/'fire_overlay.mp4')
    for name,hz,seconds in [(speech,400,1.5),(examples/'audio/ny_music.mp3',200,0.7),(examples/'audio/jingle_bells.mp3',800,0.5)]:
        ff('-f','lavfi','-i',f'sine=frequency={hz}:duration={seconds}','-c:a','libmp3lame',name)
    Image.new('RGB',photo_size,'blue').save(child)
    await render_ded_moroz(video,child,speech,examples,out,"Маша: 100% 'солнышко'")
    result=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(out)]))
    v=next(s for s in result['streams'] if s['codec_type']=='video');a=next(s for s in result['streams'] if s['codec_type']=='audio')
    assert (v['width'],v['height'],v['codec_name'],v['r_frame_rate'])==(1920,1080,'h264','30/1')
    assert a['codec_name']=='aac' and a['channels']==2
    assert INTRO_SECONDS+1.4<float(result['format']['duration'])<INTRO_SECONDS+1.8
    values=[];background=[]
    for n,t in enumerate((INTRO_SECONDS+.1,INTRO_SECONDS+.7,INTRO_SECONDS+1.4)):
        frame=tmp_path/f'frame{n}.png';ff('-ss',t,'-i',out,'-frames:v','1',frame)
        with Image.open(frame) as im:
            rgb=im.convert('RGB');values.append(rgb.getpixel(border_pixel));background.append(rgb.getpixel((100,900)))
            if n==1:
                # The old left edge is clear: the photo/frame now takes less space.
                assert max(rgb.getpixel((1382,800)))<30
                assert rgb.getpixel((1600,800))[2]>150
    # A wider gold frame appears only during the middle interval.
    red,green,blue=values[1]
    assert red>100 and green>60 and red>blue*1.3
    assert min(values[0])<50 and min(values[2])<50
    # A red fire_overlay file exists in half the cases, but must NEVER change the background.
    assert all(max(pixel)<30 for pixel in background)
    assert (tmp_path/'result-intro.txt').read_text()=="Новогоднее поздравление\n"+recipient_line("Маша: 100% 'солнышко'")+"\nот Деда Мороза"
    assert not (tmp_path/'title.txt').exists()
    if not with_fire and photo_size==(500,500):
        # 400 Hz belongs only to the voice; it must start AFTER the intro.
        import array,math
        def voice_level(t):
            samples=array.array('f');samples.frombytes(subprocess.check_output([
                'ffmpeg','-v','error','-ss',str(t),'-i',str(out),'-t','0.25',
                '-ar','8000','-ac','1','-f','f32le','-']))
            real=sum(v*math.cos(2*math.pi*400*i/8000) for i,v in enumerate(samples))
            imag=sum(v*math.sin(2*math.pi*400*i/8000) for i,v in enumerate(samples))
            return 2*math.hypot(real,imag)/len(samples)
        before,after=voice_level(.8),voice_level(INTRO_SECONDS+.8)
        assert after>.02 and before<after*.2


@pytest.mark.parametrize('video_seconds,speech_seconds',[(0.6,2.1),(2.2,0.7)])
async def test_render_follows_actual_narration_duration_after_provider_change(tmp_path,video_seconds,speech_seconds):
    from media_service import audio_duration
    examples=tmp_path/'examples';(examples/'audio').mkdir(parents=True)
    video=tmp_path/'source.mp4';speech=tmp_path/'fish.mp3';child=tmp_path/'child.png';out=tmp_path/'out.mp4'
    ff('-f','lavfi','-i',f'color=c=black:s=320x180:r=30:d={video_seconds}',
       '-c:v','libx264','-pix_fmt','yuv420p',video)
    for path in (speech,examples/'audio/ny_music.mp3',examples/'audio/jingle_bells.mp3'):
        ff('-f','lavfi','-i',f'sine=frequency=440:duration={speech_seconds}','-c:a','libmp3lame',path)
    Image.new('RGB',(100,100),'blue').save(child)
    expected=await audio_duration(speech)+INTRO_SECONDS
    await render_ded_moroz(video,child,speech,examples,out,'Машенька')
    result=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(out)]))
    assert abs(float(result['format']['duration'])-expected)<0.15
    assert all(abs(float(s.get('start_time',0)))<0.05 for s in result['streams'])
    assert next(s for s in result['streams'] if s['codec_type']=='audio')['codec_name']=='aac'

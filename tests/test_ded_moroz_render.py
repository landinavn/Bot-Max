"""Real offline FFmpeg: landscape, screen overlay, timed rectangular PiP and AAC."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from video_service import render_ded_moroz


def ff(*args):
    subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*map(str,args)],check=True,capture_output=True)


@pytest.mark.parametrize('with_fire',[True,False])
@pytest.mark.parametrize('photo_size,border_pixel',[((500,500),(1382,542)),((941,1672),(1382,170))])
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
    assert 1.4<float(result['format']['duration'])<1.8
    values=[];background=[]
    for n,t in enumerate((0.1,0.7,1.4)):
        frame=tmp_path/f'frame{n}.png';ff('-ss',t,'-i',out,'-frames:v','1',frame)
        with Image.open(frame) as im:
            rgb=im.convert('RGB');values.append(rgb.getpixel(border_pixel));background.append(rgb.getpixel((100,900)))
    # The white 6px rectangular frame appears only during the middle interval.
    assert min(values[1])>200
    assert min(values[0])<50 and min(values[2])<50
    if with_fire:
        assert background[0][0]>180
    else:
        assert max(background[0])<30
        assert 'fire_overlay.mp4' in caplog.text and 'пропущено' in caplog.text
    assert (tmp_path/'title.txt').read_text()=="Для Маша: 100% 'солнышко' от Деда Мороза"

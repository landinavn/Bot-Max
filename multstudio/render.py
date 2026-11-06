"""Offline FFmpeg editing. Source files and intermediate renders are retained."""
import asyncio
import json
import shutil
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont


async def process(*args, cwd=None, timeout=900):
    child = await asyncio.create_subprocess_exec(*map(str,args), cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, error = await asyncio.wait_for(child.communicate(), timeout)
    except BaseException:
        if child.returncode is None:
            child.kill()
        await child.wait()
        raise
    if child.returncode:
        raise ValueError('Ошибка локального FFmpeg/FFprobe; исходники сохранены.')
    return out


async def probe(path):
    return json.loads(await process('ffprobe','-v','error','-show_format','-show_streams','-of','json',path))


async def duration(path):
    return float((await probe(path))['format']['duration'])


class Renderer:
    def __init__(self, assets):
        self.assets = assets

    def ready(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise ValueError('Установите ffmpeg и ffprobe на сервере.')

    async def speech(self, paths):
        self.ready()
        result = self.assets.path(uuid4().hex + '.mp3')
        args = ['ffmpeg','-nostdin','-v','error','-y']
        for path in paths:
            args += ['-i',str(path)]
        chain = ''.join(f'[{i}:a]' for i in range(len(paths)))
        await process(*args,'-filter_complex',chain+f'concat=n={len(paths)}:v=0:a=1[a]',
                      '-map','[a]','-ar','48000','-c:a','libmp3lame',result)
        return result

    @staticmethod
    def subtitle_text(scene):
        return ' '.join(x['text'] for x in scene['spec'].get('dialogue', [])).replace('\n',' ').replace('{','').replace('}','').replace('\\','')

    async def scene(self, scene, ending=None):
        self.ready()
        assets = scene['assets']
        source = assets.get('video') or assets['image']
        voice = assets.get('speech')
        seconds = float(scene['spec']['duration'])
        if voice:
            seconds = max(seconds, await duration(self.assets.path(voice['path'])) + 0.2)
        result = self.assets.path(uuid4().hex + '.mp4')
        args = ['ffmpeg','-nostdin','-v','error','-y']
        if source['kind'] == 'image':
            args += ['-loop','1','-framerate','30']
        args += ['-i',str(self.assets.path(source['path']))]
        if voice:
            args += ['-i',str(self.assets.path(voice['path']))]
        else:
            args += ['-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=48000']
        filters = f'[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration={seconds}[base]'
        current = 'base'
        text = self.subtitle_text(scene)
        if text:
            # Relative generated filename; no user input interpolated into a filter expression.
            srt = self.assets.path(uuid4().hex + '.srt')
            millis = int(seconds*1000)
            stamp = f'{millis//3600000:02}:{millis//60000%60:02}:{millis//1000%60:02},{millis%1000:03}'
            srt.write_text(f'1\n00:00:00,000 --> {stamp}\n{text}\n', encoding='utf-8')
            filters += f';[base]subtitles={srt.name}:force_style=\'FontName=DejaVu Sans,FontSize=18,MarginV=35,Outline=1\'[sub]'
            current = 'sub'
        if ending:
            overlay = self.assets.path(uuid4().hex + '.png')
            await asyncio.to_thread(self.endcard, ending, overlay)
            args += ['-loop','1','-i',str(overlay)]
            filters += f';[{current}][2:v]overlay=0:0[out]'
            current = 'out'
        filters += f';[1:a]apad,atrim=duration={seconds},aformat=sample_rates=48000:channel_layouts=stereo[a]'
        await process(*args,'-filter_complex',filters,'-map',f'[{current}]','-map','[a]',
            '-t',str(seconds),'-r','30','-c:v','libx264','-preset','veryfast','-threads','2','-crf','23',
            '-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2','-movflags','+faststart',result,cwd=self.assets.root)
        return result

    @staticmethod
    def endcard(lines, destination):
        image = Image.new('RGBA', (1080,1920), (0,0,0,0))
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 42)
        y = 1300
        for line in lines:
            # Long titles are wrapped, never shell/filter text.
            words, wrapped, current = str(line).split(), [], ''
            for word in words:
                candidate = (current+' '+word).strip()
                if draw.textlength(candidate,font=font) > 940 and current:
                    wrapped.append(current); current = word
                else:
                    current = candidate
            wrapped.append(current)
            for part in wrapped:
                draw.text((540,y),part,font=font,fill='white',anchor='mm',stroke_width=3,stroke_fill='black')
                y += 65
        image.save(destination)

    async def assemble(self, snapshot):
        self.ready()
        paths = []
        for index, scene in enumerate(snapshot['scenes']):
            ending = snapshot['spec'].get('ending') if index == len(snapshot['scenes'])-1 else None
            paths.append(await self.scene(scene, ending))
        manifest = self.assets.path(uuid4().hex + '.ffconcat')
        manifest.write_text(''.join(f"file '{p.name}'\n" for p in paths))
        result = self.assets.path(uuid4().hex + '.mp4')
        args = ['ffmpeg','-nostdin','-v','error','-y','-f','concat','-safe','1','-i',str(manifest)]
        music = snapshot['assets'].get('music')
        if music:
            args += ['-stream_loop','-1','-i',str(self.assets.path(music['path'])),
                     '-filter_complex','[1:a]volume=0.12[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]',
                     '-map','0:v','-map','[a]','-c:a','aac']
        else:
            args += ['-map','0:v','-map','0:a','-c:a','copy']
        await process(*args,'-c:v','copy','-movflags','+faststart',result,cwd=self.assets.root)
        return result

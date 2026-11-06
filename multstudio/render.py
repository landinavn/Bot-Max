"""Offline FFmpeg editing. Source files and intermediate renders are retained."""
import asyncio
import hashlib
import json
import logging
import math
import re
from fractions import Fraction
import shutil
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont
from . import repository as repo

logger = logging.getLogger(__name__)
FPS = 30
SUBTITLE_FONT_SIZE = 14
ENDCARD_FONT_SIZE = 94  # Comparable to 14 ASS points at libass's default 288-line canvas.

def stamp(value):
    ms=int(round(value*1000))
    return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'



async def process(*args, cwd=None, timeout=900, return_stderr=False):
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
    return error if return_stderr else out


async def probe(path):
    return json.loads(await process('ffprobe','-v','error','-show_format','-show_streams','-of','json',path))


async def duration(path):
    info=await probe(path)
    # Container duration can include a nonzero start timestamp or an unrelated
    # audio track. Measure the selected track that will actually be rendered.
    streams=info.get('streams',[])
    stream=next((x for x in streams if x.get('codec_type')=='video'),None)
    stream=stream or next((x for x in streams if x.get('codec_type')=='audio'),{})
    values=[stream.get('duration')]
    try:
        values.append(float(stream['duration_ts'])*float(Fraction(stream['time_base'])))
    except (KeyError,ValueError,TypeError,ZeroDivisionError):
        pass
    values.append(info.get('format',{}).get('duration'))
    for value in values:
        try:
            seconds=float(value)
            if math.isfinite(seconds) and seconds>0: return seconds
        except (TypeError,ValueError,OverflowError):
            pass
    raise ValueError('Не удалось определить длительность сохранённого медиафайла; исходники сохранены.')


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
        if not paths:
            raise ValueError('Нет сохранённых реплик для сборки озвучки.')
        filters=';'.join(f'[{i}:a:0]asetpts=PTS-STARTPTS,aresample=48000,'
                         f'aformat=sample_rates=48000:channel_layouts=stereo[part{i}]' for i in range(len(paths)))
        chain = ''.join(f'[part{i}]' for i in range(len(paths)))
        await process(*args,'-filter_complex',filters+';'+chain+f'concat=n={len(paths)}:v=0:a=1[a]',
                      '-map','[a]','-ar','48000','-c:a','libmp3lame',result)
        return result

    async def speech_timing(self, path):
        """Remove only a long silent prefix from the render, never from the source."""
        seconds=await duration(path)
        report=await process('ffmpeg','-nostdin','-hide_banner','-v','info','-i',str(path),
            '-map','0:a:0','-af','asetpts=N/SR/TB,silencedetect=noise=-50dB:d=0.3',
            '-f','null','-',return_stderr=True)
        events=re.finditer(r'silence_(start|end):\s*([-+0-9.eE]+)',report.decode(errors='replace'))
        initial=False
        for event in events:
            kind,value=event.group(1),float(event.group(2))
            if not math.isfinite(value):
                raise ValueError('Некорректная проверка начальной тишины озвучки.')
            if kind=='start':
                if value>0.001:
                    break  # Speech has already begun; keep ALL interior pauses.
                initial=True
            elif initial:
                if value>=seconds-0.05:
                    raise ValueError('Сохранённая озвучка содержит только тишину. Выберите другой аудиорезультат; исходник сохранён.')
                # Keep 20 ms before the first sound to protect a soft consonant/breath.
                trim=max(0.0,value-0.02)
                logger.info('Монтаж озвучки: начальная тишина %.3f с, убрано %.3f с; исходник сохранён',value,trim)
                return trim,max(0.0,seconds-trim)
        return 0.0,seconds

    @staticmethod
    def subtitle_text(scene):
        return ' '.join(x['text'] for x in scene['spec'].get('dialogue', [])).replace('\n',' ').replace('{','').replace('}','').replace('\\','')

    async def scene(self, scene, ending=None):
        self.ready()
        assets = scene['assets']
        source = assets.get('video') or assets['image']
        voice = assets.get('speech')
        offset=repo.speech_start(scene.get('editing',{}).get('speech_start',0))
        trim=repo.video_start(scene.get('editing',{}).get('video_start',0))
        source_seconds=0.0 if source['kind']=='image' else await duration(self.assets.path(source['path']))
        if trim and source['kind']=='image':
            raise ValueError('Начало видео можно обрезать только у готового ролика, а не картинки.')
        if trim and trim>=source_seconds:
            raise ValueError('Начало видео находится за концом ролика. Укажите время меньше его длительности.')
        video_seconds=max(0.0,source_seconds-trim)
        voice_trim,voice_seconds=await self.speech_timing(self.assets.path(voice['path'])) if voice else (0.0,0.0)
        longest=max(max(0.0,float(scene['spec']['duration'])-trim),video_seconds,offset+voice_seconds+0.2 if voice else 0)
        # An integral video frame count also gives an integral 48 kHz sample count.
        seconds=math.ceil(longest*FPS-1e-9)/FPS
        extension=max(0.0,seconds-video_seconds) if source['kind']!='image' else 0.0
        logger.info('Монтаж сцены %s: video=%.3f speech=%.3f start=%.3f output=%.3f freeze=%.3f cut=%.3f',
                    scene.get('number',scene.get('id','?')),video_seconds,voice_seconds,offset,seconds,extension,trim)
        result = self.assets.path(uuid4().hex + '.mp4')
        args = ['ffmpeg','-nostdin','-v','error','-y']
        if source['kind'] == 'image':
            args += ['-loop','1','-framerate','30']
        args += ['-i',str(self.assets.path(source['path']))]
        if voice:
            args += ['-i',str(self.assets.path(voice['path']))]
        else:
            args += ['-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=48000']
        filters = (f'[0:v:0]setpts=PTS-STARTPTS,trim=start={trim:.9f},setpts=PTS-STARTPTS,scale=1080:1920:force_original_aspect_ratio=increase,'
                   f'crop=1080:1920,setsar=1,fps={FPS},tpad=stop_mode=clone:stop_duration={extension:.9f},'
                   f'trim=duration={seconds:.9f},setpts=PTS-STARTPTS[base]')
        current = 'base'
        text = self.subtitle_text(scene)
        if text:
            # Relative generated filename; no user input interpolated into a filter expression.
            srt = self.assets.path(uuid4().hex + '.srt')
            end=min(seconds,offset+voice_seconds) if voice else seconds
            start=offset if voice else 0
            srt.write_text(f'1\n{stamp(start)} --> {stamp(end)}\n{text}\n', encoding='utf-8')
            if all(k in scene for k in ('project_id','target','id','context_hash')):
                await repo.add_asset(scene['project_id'],scene['target'],scene['id'],'subtitle',scene['context_hash'],
                    srt.name,hashlib.sha256(srt.read_bytes()).hexdigest(),job_id=scene.get('assembly_job_id'),
                    metadata=repo.encoded({'language':'ru','source':'scene.dialogue','white_text_black_outline':True}))
            filters += (f';[base]subtitles={srt.name}:force_style=\'FontName=DejaVu Sans,FontSize={SUBTITLE_FONT_SIZE},MarginV=35,'
                        'PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0\'[sub]')
            current = 'sub'
        if ending:
            overlay = self.assets.path(uuid4().hex + '.png')
            await asyncio.to_thread(self.endcard, ending, overlay)
            args += ['-loop','1','-i',str(overlay)]
            filters += f';[2:v]setpts=PTS-STARTPTS[title];[{current}][title]overlay=0:0[out]'
            current = 'out'
        delay=f',adelay={round(offset*48000)}S:all=1' if voice and offset else ''
        filters += (f';[1:a:0]asetpts=PTS-STARTPTS,atrim=start={voice_trim:.9f},asetpts=N/SR/TB,aresample=48000,'
                    f'aformat=sample_rates=48000:channel_layouts=stereo{delay},asetpts=N/SR/TB,apad,'
                    f'atrim=duration={seconds:.9f},asetpts=N/SR/TB[a]')
        await process(*args,'-filter_complex',filters,'-map',f'[{current}]','-map','[a]',
            '-t',str(seconds),'-r','30','-c:v','libx264','-preset','veryfast','-threads','2','-crf','23',
            '-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2','-movflags','+faststart',result,cwd=self.assets.root)
        return result

    @staticmethod
    def endcard(lines, destination):
        image = Image.new('RGBA', (1080,1920), (0,0,0,0))
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', ENDCARD_FONT_SIZE)
        wrapped=[]
        for line in lines:
            current=''
            # Keep even a single long word inside the safe area.
            for word in str(line).split():
                candidate=(current+' '+word).strip()
                if draw.textlength(candidate,font=font)>920 and current:
                    wrapped.append(current); current=''
                for char in word:
                    candidate=current+char
                    if draw.textlength(candidate,font=font)>920 and current:
                        wrapped.append(current); current=''
                    current+=char
                current+=' '
            wrapped.append(current.strip())
        line_height=ENDCARD_FONT_SIZE+24
        y=max(120,min(1300,1720-len(wrapped)*line_height))
        for line in wrapped:
            draw.text((540,y),line,font=font,fill='white',anchor='mt',stroke_width=4,stroke_fill='black')
            y+=line_height
        image.save(destination)

    async def assemble(self, snapshot):
        self.ready()
        paths, lengths, cues, elapsed = [], [], [], 0.0
        for index, scene in enumerate(snapshot['scenes']):
            ending = snapshot['spec'].get('ending') if index == len(snapshot['scenes'])-1 else None
            path=await self.scene(dict(scene,assembly_job_id=snapshot.get('job_id')), ending)
            paths.append(path)
            seconds=await duration(path)
            lengths.append(seconds)
            text=self.subtitle_text(scene)
            if text:
                voice=scene['assets'].get('speech')
                start=repo.speech_start(scene.get('editing',{}).get('speech_start',0)) if voice else 0
                if voice:
                    _,voice_seconds=await self.speech_timing(self.assets.path(voice['path']))
                    end=min(seconds,start+voice_seconds)
                else:
                    end=seconds
                cues.append(f'{len(cues)+1}\n{stamp(elapsed+start)} --> {stamp(elapsed+end)}\n{text}\n')
            elapsed += seconds
        episode_srt=self.assets.path(uuid4().hex+'.srt')
        episode_srt.write_text('\n'.join(cues),encoding='utf-8')
        if all(k in snapshot for k in ('project_id','target','id','context_hash')):
            await repo.add_asset(snapshot['project_id'],snapshot['target'],snapshot['id'],'subtitle',snapshot['context_hash'],
                episode_srt.name,hashlib.sha256(episode_srt.read_bytes()).hexdigest(),job_id=snapshot.get('job_id'),
                metadata=repo.encoded({'language':'ru','timeline':'episode','duration':elapsed}))
        manifest = self.assets.path(uuid4().hex + '.ffconcat')
        manifest.write_text(''.join(f"file '{p.name}'\nduration {seconds:.9f}\n" for p,seconds in zip(paths,lengths)))
        result = self.assets.path(uuid4().hex + '.mp4')
        # Preserve the zero video PTS; concat's negative AAC priming start would
        # otherwise shift all video forward by about 21 ms before audio filtering.
        args = ['ffmpeg','-nostdin','-v','error','-y','-copyts','-f','concat','-safe','1','-i',str(manifest)]
        # Copy the uniform video frames, but rebuild audio from exact scene
        # sample ranges. Copying independent AAC packets accumulates encoder priming
        # and tail padding at scene boundaries and shifts later speech.
        for path in paths:
            args+=['-i',str(path)]
        filters=[]
        for i,seconds in enumerate(lengths,1):
            filters.append(f'[{i}:a:0]asetpts=PTS-STARTPTS,aresample=48000,'
                f'aformat=sample_rates=48000:channel_layouts=stereo,apad,'
                f'atrim=duration={seconds:.9f},asetpts=N/SR/TB[clip{i}]')
        filters.append(''.join(f'[clip{i}]' for i in range(1,len(paths)+1))
                       +f'concat=n={len(paths)}:v=0:a=1[voice]')
        music = snapshot['assets'].get('music')
        current='voice'
        if music:
            args += ['-stream_loop','-1','-i',str(self.assets.path(music['path']))]
            filters.append(f'[{len(paths)+1}:a:0]asetpts=PTS-STARTPTS,aresample=48000,'
                'aformat=sample_rates=48000:channel_layouts=stereo,volume=0.12[m]')
            filters.append('[voice][m]amix=inputs=2:duration=first:normalize=0[a]')
            current='a'
        await process(*args,'-filter_complex',';'.join(filters),'-map','0:v:0','-map',f'[{current}]',
            '-c:v','copy','-c:a','aac','-ar','48000','-ac','2','-t',f'{elapsed:.9f}',
            '-movflags','+faststart',result,cwd=self.assets.root)
        return result

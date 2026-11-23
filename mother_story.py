"""Seven retained five-second shots; only explicit, checkpointed paid submissions."""
import asyncio
import shutil
import os
import time
from uuid import uuid4
import tempfile
from pathlib import Path

import httpx

POLICY = 'mother-one-character-heygen-seven-shots-v1'
SHOTS = (
    ('Средний план: мама ставит цветы в вазу', 'Medium shot. Mother gently places a bouquet into a vase on the dining table.'),
    ('Цветы и улыбка мамы', 'Medium side shot including her face and hands. Mother arranges the flowers, then smiles at them.'),
    ('Общий план: мама накрывает стол', 'Wide shot. Mother sets plates on the dining table, moving naturally around it.'),
    ('Мама завершает сервировку', 'Medium shot. Mother folds a napkin and carefully sets the last cup on the table.'),
    ('Мама приглашает к столу', 'Medium shot. Mother turns toward the doorway and makes a warm inviting hand gesture toward the table. Guests remain off-screen.'),
    ('Мама открывает фотоальбом', 'Medium seated shot. Mother opens a family photo album at the same dining table.'),
    ('Тёплые воспоминания', 'Over-the-shoulder medium shot. Mother slowly turns an album page, then looks up with a tender smile. Album images are indistinct decorative shapes, no legible faces.'),
)
BASE = (
    'Single continuous high-quality stylized 3D animated family movie, horizontal 16:9 frame. Keep her entire head comfortably inside the central widescreen safe area. '
    'EXACTLY ONE visible character: the same adult mother as in the identity reference. '
    'Preserve her age, recognizable face, hair, outfit and proportions. '
    'Keep the exact same cozy apartment, dining table, vase, furniture, sunset lighting and color palette '
    'as in the location reference. No children, other adults, crowds, reflections of people or extra faces. '
    'One undivided frame, no collage, split screen, inset or montage. No text, subtitles, logos or watermark. '
    'Mother does not talk or sing; closed relaxed mouth, no lip sync. Background song will be added later. '
)


async def submit_animation(adapter,image_url,prompt,key):
    from heygen import https_url
    https_url(image_url)
    # Same image-to-video API contract used by the existing silent studio scenes.
    data=await adapter.request('POST','/v3/models/videos',payload={
        'model':'heygen-video-1','mode':'image_to_video',
        'image':{'type':'url','url':image_url},'prompt':prompt,
        'duration':5,'resolution':'1080p','prompt_enhancement':'disabled'},idempotency=key)
    return adapter.identifier(data.get('video_id') or data.get('id'))


async def poll_animation(adapter,provider_id,*,timeout=1200):
    from heygen import HeyGenError,https_url
    identifier=adapter.identifier(provider_id)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            data=await adapter.request('GET','/v3/models/videos/'+identifier)
        except (httpx.RequestError,HeyGenError) as exc:
            if isinstance(exc,HeyGenError) and exc.status_code not in (429,500,502,503,504):raise
            await asyncio.sleep(10);continue
        if data.get('status') in ('failed','cancelled','error'):
            raise HeyGenError('HeyGen не завершил сцену. Нужна проверка задачи; новый платный запрос не отправлялся.')
        if data.get('status')=='completed':
            url=data.get('video_url','');https_url(url);return url
        await asyncio.sleep(5)
    raise TimeoutError('Ожидание сцены HeyGen истекло. ID сохранён, новый платный запрос не отправляется.')


async def render_local(clips, audio, target, duration):
    from media_service import run_media_process,MediaServiceError
    if len(clips)!=len(SHOTS) or not 30<=duration<=35:
        raise ValueError('Нужны семь сцен и песня продолжительностью не менее 30 секунд.')
    binary=shutil.which('ffmpeg')
    if not binary:raise MediaServiceError('Для мультфильма установите ffmpeg.')
    inputs=[];filters=[]
    for i,path in enumerate(clips):
        inputs+=['-i',str(path)]
        filters.append(f'[{i}:v]setpts=PTS-STARTPTS,fps=25,scale=1280:720:force_original_aspect_ratio=increase,'
                       f'crop=1280:720,setsar=1,'
                       f'tpad=stop_mode=clone:stop_duration=5,trim=duration=5,setpts=PTS-STARTPTS[v{i}]')
    filters.append(''.join(f'[v{i}]' for i in range(len(clips)))+f'concat=n={len(clips)}:v=1:a=0,trim=duration={duration}[v]')
    filters.append(f'[{len(clips)}:a]asetpts=PTS-STARTPTS,aresample=48000,apad,atrim=duration={duration},'
                   f'afade=t=out:st={duration-1}:d=1[a]')
    await run_media_process(binary,'-nostdin','-v','error','-y','-filter_complex_threads','1',
        '-protocol_whitelist','file,pipe',*inputs,'-i',str(audio),'-filter_complex',';'.join(filters),
        '-map','[v]','-map','[a]','-c:v','libx264','-threads','1','-preset','fast','-crf','22',
        '-pix_fmt','yuv420p','-r','25','-c:a','aac','-b:a','192k','-t',str(duration),
        '-movflags','+faststart','-map_metadata','-1','-map_chapters','-1',str(target),timeout=600)
    if not target.is_file() or not target.stat().st_size:raise MediaServiceError('Не удалось собрать мультфильм.')


async def generate(data, reference_url, audio_url, *, checkpoint, progress):
    """checkpoint persists the entire storyboard in the existing paid-order FSM."""
    from config import Config
    from image_service import generate_photosession_frame
    from heygen import HeyGenAdapter
    from media_service import _download,audio_duration,_storage_client,_upload_video,MediaServiceError
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise MediaServiceError('Для мультфильма установите ffmpeg и ffprobe.')
    source=[POLICY,reference_url]
    story=data.get('cartoon_kling_story')
    if story is not None and story.get('source')!=source:
        raise ValueError('В этом заказе уже сохранён другой аватар. Обратитесь в поддержку; сцены сохранены.')
    if story is None:
        story={'source':source,'shots':[{} for _ in SHOTS]}
        data['cartoon_kling_story']=story
        await checkpoint()
    if story.get('result_audio')==audio_url and story.get('result_url'):return story['result_url']
    # Validate audio/storage before any new image/video charges.
    settings=Config.from_env()
    key=os.getenv('HEYGEN_API_KEY','').strip()
    if not key:raise ValueError('Задайте HEYGEN_API_KEY для мульт-открытки.')
    animation=HeyGenAdapter(key)
    client=await asyncio.to_thread(_storage_client,settings)
    try:
        with tempfile.TemporaryDirectory(prefix='mother-story-') as directory:
            root=Path(directory);audio=root/'song.mp3'
            async with httpx.AsyncClient(timeout=120,trust_env=False,follow_redirects=False) as http:
                await _download(http,audio_url,audio,32*1024*1024)
                seconds=await audio_duration(audio)
                if seconds<30:raise ValueError('Песня короче 30 секунд. Выберите другой вариант до создания мультфильма.')
                duration=min(35.,seconds)
                for index,(label,action) in enumerate(SHOTS):
                    shot=story['shots'][index]
                    await progress(f'🎬 Создаю сцену {index+1} из {len(SHOTS)}: {label.lower()}. Пожалуйста, подождите.')
                    if not shot.get('image_url'):
                        if shot.get('image_submitting'):
                            raise ValueError('Результат запроса картинки неизвестен. Нужна проверка поддержки; повторная генерация не запускалась.')
                        shot['image_submitting']=True;await checkpoint()
                        previous=story['shots'][0].get('image_url') or reference_url
                        shot['image_url']=await generate_photosession_frame(reference_url,BASE+action,
                            style_reference_url=previous,image_size='1536x1024')
                        await asyncio.shield(checkpoint())
                    if not shot.get('task_id'):
                        if shot.get('video_submitting'):
                            raise ValueError('Результат запроса анимации неизвестен. Нужна проверка поддержки; повторный запрос не отправлялся.')
                        shot['video_submitting']=True
                        shot.setdefault('request_key','mother-scene-'+uuid4().hex)
                        await checkpoint()
                        shot['task_id']=await submit_animation(animation,shot['image_url'],BASE+action+' Natural continuous motion for five seconds.',shot['request_key'])
                        await asyncio.shield(checkpoint())
                    if not shot.get('video_url'):
                        shot['video_url']=await poll_animation(animation,shot['task_id'],timeout=1200)
                        await checkpoint()
                clips=[]
                for i,shot in enumerate(story['shots']):
                    path=root/f'scene-{i}.mp4'
                    await _download(http,shot['video_url'],path,100*1024*1024);clips.append(path)
            await progress('🎵 Все сцены готовы! Добавляю вашу песню и собираю мультфильм.')
            target=root/'mother.mp4';await render_local(clips,audio,target,duration)
            actual=await audio_duration(target)
            if not 30<=actual<=40:raise MediaServiceError('Не удалось выдержать длительность мультфильма 30–40 секунд.')
            story['result_url']=await asyncio.to_thread(_upload_video,client,settings,target)
            story['result_audio']=audio_url
            await checkpoint()
            return story['result_url']
    finally:
        await asyncio.to_thread(client.close)

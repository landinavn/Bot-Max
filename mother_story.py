"""Six genuine 10-second shots, vocal start alignment, retained paid IDs and legacy resume."""
import asyncio
import shutil
import os
import time
from uuid import uuid4
import tempfile
from pathlib import Path

import httpx

from mother_storyboard import POLICY,SHOTS,BASE,SHOT_SECONDS,DURATION,image_prompt,video_prompt
import mother_story_legacy as legacy


async def submit_animation(adapter,image_url,prompt,key,*,duration=SHOT_SECONDS):
    from heygen import https_url
    https_url(image_url)
    # Same image-to-video API contract used by the existing silent studio scenes.
    data=await adapter.request('POST','/v3/models/videos',payload={
        'model':'heygen-video-1','mode':'image_to_video',
        'image':{'type':'url','url':image_url},'prompt':prompt,
        'duration':duration,'resolution':'1080p','aspect_ratio':'16:9','prompt_enhancement':'disabled'},idempotency=key)
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


async def video_duration(path):
    from media_service import run_media_process,MediaServiceError
    import json,math
    raw=await run_media_process(shutil.which('ffprobe'),'-v','error','-select_streams','v:0',
        '-show_entries','stream=duration','-of','json',str(path),timeout=30)
    try:
        duration=float(json.loads(raw)['streams'][0]['duration'])
        if not math.isfinite(duration) or duration<=0:raise ValueError
        return duration
    except (ValueError,KeyError,IndexError,TypeError):
        raise MediaServiceError('Не удалось проверить длительность видеоряда.') from None


async def extract_last_frame(video,target):
    from media_service import run_media_process
    await run_media_process(shutil.which('ffmpeg'),'-nostdin','-v','error','-y','-sseof','-0.08',
        '-i',str(video),'-frames:v','1',str(target),timeout=60)
    if not target.is_file():raise ValueError('Не удалось сохранить кадр для продолжения сцены.')


async def render_local(clips,audio,target,duration,*,shot_seconds=SHOT_SECONDS,audio_start=0.):
    from media_service import run_media_process,MediaServiceError
    old=len(clips)==len(legacy.SHOTS) and shot_seconds==5 and 30<=duration<=35
    new=len(clips)==len(SHOTS) and shot_seconds==10 and duration==60
    if not (old or new) or audio_start<0:raise ValueError('Неверная длительность сцен мультфильма.')
    binary=shutil.which('ffmpeg')
    if not binary:raise MediaServiceError('Для мультфильма установите ffmpeg.')
    inputs=[];filters=[]
    for i,path in enumerate(clips):
        inputs+=['-i',str(path)]
        # The foreground is never zoomed/cropped. Fill any aspect-ratio margins with a soft backdrop.
        filters.append(f'[{i}:v]setpts=PTS-STARTPTS,fps=25,split=2[bg{i}][fg{i}]')
        filters.append(f'[bg{i}]scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,boxblur=20:1[b{i}]')
        filters.append(f'[fg{i}]scale=1280:720:force_original_aspect_ratio=decrease[f{i}]')
        filters.append(f'[b{i}][f{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,'
            f'tpad=stop_mode=clone:stop_duration=0.25,trim=duration={shot_seconds},setpts=PTS-STARTPTS[v{i}]')
    filters.append(''.join(f'[v{i}]' for i in range(len(clips)))+f'concat=n={len(clips)}:v=1:a=0,trim=duration={duration}[v]')
    filters.append(f'[{len(clips)}:a]atrim=start={audio_start},asetpts=PTS-STARTPTS,aresample=48000,'
        f'apad,atrim=duration={duration},afade=t=out:st={duration-1}:d=1[a]')
    await run_media_process(binary,'-nostdin','-v','error','-y','-filter_complex_threads','1',
        '-protocol_whitelist','file,pipe',*inputs,'-i',str(audio),'-filter_complex',';'.join(filters),
        '-map','[v]','-map','[a]','-c:v','libx264','-threads','1','-preset','fast','-crf','22',
        '-pix_fmt','yuv420p','-r','25','-c:a','aac','-b:a','192k','-t',str(duration),
        '-movflags','+faststart','-map_metadata','-1','-map_chapters','-1',str(target),timeout=600)
    if not target.is_file() or not target.stat().st_size:raise MediaServiceError('Не удалось собрать мультфильм.')


async def generate(data, reference_url, audio_url, *, checkpoint, progress):
    """checkpoint persists the entire storyboard in the existing paid-order FSM."""
    from config import Config
    from image_service import generate_photosession_frame,_store_image
    from heygen import HeyGenAdapter
    from media_service import _download,audio_duration,_storage_client,_upload_video,MediaServiceError
    from song_alignment import resolve_song_start
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise MediaServiceError('Для мультфильма установите ffmpeg и ffprobe.')
    story=data.get('cartoon_kling_story')
    old=bool(story and story.get('source',[None])[0]==legacy.POLICY)
    policy=legacy.POLICY if old else POLICY
    shots=legacy.SHOTS if old else SHOTS
    shot_seconds=5 if old else SHOT_SECONDS
    source=[policy,reference_url]
    if story is not None and story.get('source')!=source:
        raise ValueError('В этом заказе уже сохранён другой аватар. Обратитесь в поддержку; сцены сохранены.')
    if story is None:
        story={'source':source,'shot_seconds':shot_seconds,'shots':[{} for _ in shots]}
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
                minimum=30 if old else DURATION
                if seconds<minimum:raise ValueError(f'Песня короче {minimum} секунд. Нужна полная песня до создания сцен.')
                audio_start=0.
                if not old:
                    lyrics=data.get('song_review_lyrics') or data.get('video_song_lyrics') or ''
                    await progress('🎵 Подготавливаю песню для мультфильма. Готовая музыка и картинка сохранены.')
                    audio_start = await resolve_song_start(story, audio, lyrics, audio_url, root, checkpoint)
                    if seconds-audio_start<DURATION:
                        raise ValueError('После музыкального вступления осталось меньше минуты песни. Сцены ещё не создавались.')
                duration=min(35.,seconds) if old else float(DURATION)
                clips=[]
                for index,item in enumerate(shots):
                    label=item[0] if old else item['label']
                    frame_prompt=legacy.BASE+item[1] if old else image_prompt(index)
                    motion_prompt=(legacy.BASE+item[1]+' Natural continuous motion for five seconds.') if old else video_prompt(index)
                    shot=story['shots'][index]
                    shot.setdefault('image_prompt',frame_prompt);shot.setdefault('video_prompt',motion_prompt)
                    await progress(f'🎬 Создаю сцену {index+1} из {len(shots)}: {label.lower()}. Пожалуйста, подождите.')
                    if not shot.get('image_url'):
                        if shot.get('image_submitting'):
                            raise ValueError('Результат запроса картинки неизвестен. Нужна проверка поддержки; повторная генерация не запускалась.')
                        shot['image_submitting']=True;await checkpoint()
                        location=story['shots'][0].get('image_url') or reference_url
                        if old:
                            shot['image_url']=await generate_photosession_frame(reference_url,shot['image_prompt'],
                                style_reference_url=location,image_size='1536x1024')
                        else:
                            continuity=story['shots'][index-1].get('end_frame_url') if index else None
                            refs=tuple(dict.fromkeys(x for x in (location,continuity) if x))
                            shot['image_url']=await generate_photosession_frame(reference_url,shot['image_prompt'],
                                style_reference_urls=refs,image_size='1536x1024')
                        await asyncio.shield(checkpoint())
                    if not shot.get('task_id'):
                        if shot.get('video_submitting'):
                            raise ValueError('Результат запроса анимации неизвестен. Нужна проверка поддержки; повторный запрос не отправлялся.')
                        shot['video_submitting']=True
                        shot.setdefault('request_key','mother-scene-'+uuid4().hex)
                        await checkpoint()
                        shot['task_id']=await submit_animation(animation,shot['image_url'],shot['video_prompt'],shot['request_key'],duration=shot_seconds)
                        await asyncio.shield(checkpoint())
                    if not shot.get('video_url'):
                        shot['video_url']=await poll_animation(animation,shot['task_id'],timeout=1200)
                        await checkpoint()
                    path=root/f'scene-{index}.mp4'
                    await _download(http,shot['video_url'],path,100*1024*1024)
                    if not old:
                        if await video_duration(path)<9.75:
                            raise ValueError('Получена сцена короче 10 секунд. ID сохранён; повторная генерация не запускалась.')
                        if index<len(shots)-1 and not shot.get('end_frame_url'):
                            last=root/f'end-{index}.png'
                            await extract_last_frame(path,last)
                            shot['end_frame_url']=await asyncio.to_thread(_store_image,client,settings,last)
                            await checkpoint()
                    clips.append(path)
            await progress('🎵 Все сцены готовы! Добавляю вашу песню и собираю мультфильм.')
            target=root/'mother.mp4';await render_local(clips,audio,target,duration,shot_seconds=shot_seconds,audio_start=audio_start)
            actual=await video_duration(target)
            if not ((30<=actual<=40) if old else abs(actual-DURATION)<0.2):
                raise MediaServiceError('Итоговая длительность мультфильма не соответствует сценарию.')
            story['result_url']=await asyncio.to_thread(_upload_video,client,settings,target)
            story['result_audio']=audio_url
            await checkpoint()
            return story['result_url']
    finally:
        await asyncio.to_thread(client.close)

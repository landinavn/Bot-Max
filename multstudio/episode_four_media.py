"""Approved dream images and offline five-second montage/cutaways for revised episode 4."""
import asyncio
import io
import json
import math
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import repository as repo


async def attach(db, snap, episode):
    ids=episode['spec'].get('dream_scene_ids',[])
    if len(ids)!=3 or len(set(ids))!=3: raise ValueError('Нужны три разных утверждённых изображения мечтаний.')
    snap['dream_references']=[]
    for key in ids:
        row=await repo.one(db,'''SELECT s.id FROM cartoon_scenes s JOIN cartoon_episodes e ON e.id=s.episode_id
            WHERE s.id=? AND e.project_id=? AND e.number<=?''',(key,snap['project_id'],episode['number']))
        if not row or key==snap['id']: raise ValueError('Изображения мечтаний должны принадлежать этому проекту.')
        source=await repo._snapshot(db,'scene',key)
        image=source['assets'].get('image')
        snap['dream_references'].append(dict(scene_id=key,title=source['spec']['title'],image=image if image and image.get('approved') else None))
    cutaway=snap['spec'].get('tail_cutaway_scene_id')
    if cutaway:
        row=repo.unpack(await repo.one(db,'SELECT * FROM cartoon_scenes WHERE id=? AND episode_id=?',(cutaway,episode['id'])))
        if not row or not row['spec'].get('local_dream_image'): raise ValueError('Нужна локальная карта желаний этой серии для немого плана.')
        source=await repo._snapshot(db,'scene',cutaway)
        image=source['assets'].get('image')
        snap['cutaway_reference']=image if image and image.get('approved') else None


def validate(snap, kind):
    if snap['spec'].get('material_only') and kind in ('video','speech'):
        raise ValueError('Это исходное изображение для карты; озвучка и видео для него не нужны.')
    if kind=='video' and snap['spec'].get('video_provider')=='openai':
        from .episode_four_revision import OPENAI_BLOCK
        raise ValueError(OPENAI_BLOCK)
    if snap['spec'].get('kling_audio_required') and kind in ('image','speech'):return
    if snap['spec'].get('dream_materials_required') or snap['spec'].get('tail_cutaway_scene_id'):
        if len(snap.get('dream_references',[]))!=3 or any(not r.get('image') for r in snap['dream_references']):
            raise ValueError('Сначала подготовьте и утвердите три изображения мечтаний в «Материалы карты». Пустой шаблон не используется.')
        if snap['spec'].get('tail_cutaway_scene_id') and not snap.get('cutaway_reference'):
            raise ValueError('Сначала создайте и утвердите карту желаний — изображение сцены 4. Оно заполняет немой хвост после речи.')


def card(paths):
    if len(paths)!=3: raise ValueError('Нужны три изображения мечтаний.')
    image=Image.new('RGB',(1080,1920),'#f4e8d5');draw=ImageDraw.Draw(image)
    # Leave a separate bottom area for the final Russian titles, away from photographs.
    for path,box in zip(paths,[(48,74,1032,730),(48,762,524,1426),(556,762,1032,1426)]):
        with Image.open(path) as src:
            photo=ImageOps.contain(ImageOps.exif_transpose(src).convert('RGB'),(box[2]-box[0],box[3]-box[1]))
        draw.rounded_rectangle((box[0]-8,box[1]-8,box[2]+8,box[3]+8),radius=16,fill='white')
        image.paste(photo,(box[0]+(box[2]-box[0]-photo.width)//2,box[1]+(box[3]-box[1]-photo.height)//2))
    return image


def card_bytes(snap,assets):
    validate(snap,'image')
    image=card([assets.path(r['image']['path']) for r in snap['dream_references']])
    data=io.BytesIO();image.save(data,format='PNG');return data.getvalue()


def end_titles(lines,destination):
    image=Image.new('RGBA',(1080,1920),(0,0,0,0));draw=ImageDraw.Draw(image)
    y=1488
    for line in lines:
        size=48
        while True:
            font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',size)
            if draw.textlength(line,font=font)<=984 or size<=24:break
            size-=2
        draw.text((540,y),line,font=font,fill='#382b22',anchor='mt')
        y+=100
    image.save(destination)


async def clip(renderer,snap):
    """No paid video calls: approved photos appear in sequence, then the complete board."""
    from .render import process,duration
    validate(snap,'video');renderer.ready()
    output=renderer.assets.path(uuid4().hex+'.mp4')
    board=renderer.assets.path(uuid4().hex+'.png')
    await asyncio.to_thread(board.write_bytes,card_bytes(snap,renderer.assets))
    args=['ffmpeg','-nostdin','-v','error','-y','-filter_complex_threads','1']
    paths=[renderer.assets.path(r['image']['path']) for r in snap['dream_references']]
    if snap['spec'].get('final_dream_card'):
        paths=[renderer.assets.path(snap['assets']['image']['path']),board]
        frames=[60,90]
    else:
        paths=[*paths,board];frames=[30,30,30,60]
    for path in paths:args+=['-loop','1','-framerate','30','-i',str(path)]
    args+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
    filters=[]
    for i,n in enumerate(frames):
        filters.append(f'[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,'
            f'zoompan=z=min(zoom+0.00018\\,1.04):d={n}:s=1080x1920:fps=30,trim=end_frame={n},setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.12:color=0xf4e8d5[v{i}]')
    filters.append(''.join(f'[v{i}]' for i in range(len(paths)))+f'concat=n={len(paths)}:v=1:a=0[v]')
    await process(*args,'-filter_complex',';'.join(filters),'-map','[v]','-map',f'{len(paths)}:a',
        '-t','5','-frames:v','150','-r','30','-c:v','libx264','-preset','veryfast','-threads','2','-pix_fmt','yuv420p',
        '-c:a','aac','-ar','48000','-ac','2','-movflags','+faststart',output)
    from .rules import video_seconds
    video_seconds(await duration(output));return output


async def splice_tail(renderer,snap,source,speech,speech_end):
    from .render import process,duration
    validate(snap,'video')
    source_seconds=await duration(source)
    cutoff=min(5.0,math.floor(source_seconds*30)/30,math.ceil((speech_end+.08)*30)/30)
    if cutoff+.035<speech_end: raise ValueError('Исходник не содержит безопасного окончания речи. Нельзя дорисовывать замерший портрет.')
    n=max(0,150-round(cutoff*30));output=renderer.assets.path(uuid4().hex+'.mp4')
    if not n:raise ValueError('После реплики не осталось времени на отдельный немой план; проверьте длину озвучки.')
    board=renderer.assets.path(snap['cutaway_reference']['path'])
    filters=(f'[0:v]setpts=PTS-STARTPTS,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,'
        f'trim=end_frame={150-n},setpts=PTS-STARTPTS[talk];'
        f'[1:v]scale=1080:1920,setsar=1,zoompan=z=min(zoom+0.00018\\,1.04):d={n}:s=1080x1920:fps=30,'
        f'trim=end_frame={n},setpts=PTS-STARTPTS[tail];[talk][tail]concat=n=2:v=1:a=0[v];'
        '[2:a]asetpts=N/SR/TB,aresample=48000,apad,atrim=duration=5,asetpts=N/SR/TB[a]')
    await process('ffmpeg','-nostdin','-v','error','-y','-filter_complex_threads','1','-i',str(source),
        '-loop','1','-framerate','30','-i',str(board),'-i',str(speech),'-filter_complex',filters,'-map','[v]','-map','[a]',
        '-t','5','-frames:v','150','-r','30','-c:v','libx264','-preset','veryfast','-threads','2','-pix_fmt','yuv420p',
        '-c:a','aac','-ar','48000','-ac','2','-movflags','+faststart',output)
    from .rules import video_seconds
    video_seconds(await duration(output))
    snap['normalization_metadata']=dict(speech_end=speech_end,cutaway_start=cutoff,cutaway_id=snap['cutaway_reference']['id'],tail_policy='approved-dream-card-no-freeze')
    return output


async def production_report(episode_id,settings):
    """Read-only estimate of remaining calls. Unknown prices are never shown as free."""
    from .episode_two import speech_profiles
    from .settings import dollars
    episode=await repo.snapshot('episode',episode_id)
    materials=[await repo.snapshot('scene',key) for key in episode['spec']['dream_scene_ids']]
    totals={k:dict(known=0,unknown=0,calls=0) for k in ('image','speech','video')}
    ready=sum(bool(s['assets'].get('image',{}).get('approved')) for s in materials)
    for snap in [*episode['scenes'],*materials]:
        kinds=('image',) if snap['spec'].get('material_only') else ('image','speech','video')
        for kind in kinds:
            if kind=='speech' and not snap['spec'].get('dialogue'):continue
            asset=snap['assets'].get(kind)
            if asset and (kind=='speech' or asset.get('approved')):continue
            try:
                q=settings.quote(kind,snap,speech_profiles=await speech_profiles(snap)) if kind=='speech' else settings.quote(kind,snap)
            except ValueError:
                totals[kind]['unknown']+=1;continue
            if q['model'].startswith(('PIL/','FFmpeg/')):continue
            totals[kind]['calls']+=1
            if q.get('estimate_available'):totals[kind]['known']+=q['estimate']
            else:totals[kind]['unknown']+=1
    labels={'image':'Изображения, включая материалы карты','speech':'Озвучка','video':'Анимация'}
    lines=[f"Дополнительных материалов карты готово: {ready} из 3. Новых платных изображений по плану: {totals['image']['calls']}.",
           'Говорящих сцен HeyGen: 4; Kling Avatar: 1 (Маша, с утверждённой озвучкой); немых локальных сцен: 2.',
           'Сборка карты желаний и два немых видеоплана: локально, без платного API.']
    for kind,total in totals.items():
        lines.append(labels[kind]+': '+('оценка неполная; известная часть ' if total['unknown'] else '')+dollars(total['known']))
    known=sum(t['known'] for t in totals.values())
    lines.append('Общая стоимость: '+('пока не определена; известная часть ' if any(t['unknown'] for t in totals.values()) else '')+dollars(known)+'.')
    return lines


def talking_end(video):
    """Use the retained transition, never a guessed two-second cut or current prompt."""
    from .render import FPS
    meta=json.loads(video.get('metadata') or '{}')
    cutoff=meta.get('cutaway_start');last=meta.get('speech_end')
    if (meta.get('tail_policy')!='approved-dream-card-no-freeze'
            or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (cutoff,last))
            or not 0<last<=cutoff<=5):
        raise ValueError('Нет проверенной границы вставки карты. Исходное видео сохранено; нужна проверка сцены.')
    # Cut before the first map frame. Keep the original speech rate and safe post-speech margin.
    end=math.floor(cutoff*FPS+1e-7)/FPS
    if end<last:raise ValueError('Граница вставки пересекает речь; обрезка слов запрещена.')
    return end

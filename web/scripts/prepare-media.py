"""Offline, allowlisted optimization only; no network/API/generation calls.
Usage: python prepare-media.py --examples /path/to/examples --mother-video /path/to/den_mama.mp4
Requires Pillow and ffmpeg. Never scans/copies other owner files.
"""
import argparse, hashlib, json, subprocess
from pathlib import Path
from PIL import Image, ImageOps
p=argparse.ArgumentParser(); p.add_argument('--examples',type=Path,required=True); p.add_argument('--mother-video',type=Path,required=True); p.add_argument('--santa-video',type=Path); p.add_argument('--banners',type=Path); p.add_argument('--logo',type=Path); a=p.parse_args()
out=Path(__file__).resolve().parents[1]/'public/media/works'; out.mkdir(parents=True,exist_ok=True)
registry=Path(__file__).resolve().parents[1]/'media/manifest.json'
manifest_by_file={x['file']:x for x in json.loads(registry.read_text())} if registry.exists() else {}
manifest=[]
def record(source, dest, kind, note=''):
    manifest.append(dict(source=source.name,file='/media/works/'+dest.name,kind=kind,sourceSha256=hashlib.sha256(source.read_bytes()).hexdigest(),bytes=dest.stat().st_size,approval=('Uploaded by owner for public Frontend 1.1 use on 2026-11-26.' if source.name=='ded_moroz_kids.mp4' or source.suffix=='.png' else 'User explicitly approved public use on 2026-11-26, including people and children.'),note=note))
def picture(source,name):
    im=ImageOps.exif_transpose(Image.open(source)).convert('RGB'); im.thumbnail((1100,1400))
    target=out/(name+'.webp'); im.save(target,'WEBP',quality=86,method=6); record(source,target,'image')
for filename,name in [('before.jpg','vision-before'),('after.jpg','vision-after'),('photo_fam_1.webp','family-1'),('photo_fam_2.webp','family-2'),('photo_fam_3.webp','family-3')]: picture(a.examples/filename,name)
videos=[(a.examples/'cartoon_example.mp4','birthday-cartoon'),(a.examples/'musical-card.mp4','musical-card'),(a.mother_video,'mother-cartoon')]
if a.santa_video: videos.append((a.santa_video,'santa-family'))
for source,name in videos:
    video=out/(name+'.mp4')
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(source),'-vf',"scale=w='min(960,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",'-c:v','libx264','-preset','fast','-crf','25','-pix_fmt','yuv420p','-c:a','aac','-b:a','96k','-movflags','+faststart',str(video)],check=True)
    record(source,video,'video')
    poster=out/(name+'.webp')
    subprocess.run(['ffmpeg','-v','error','-y','-ss',('8' if name=='santa-family' else '1'),'-i',str(source),'-frames:v','1','-vf',"scale=w='min(960,iw)':h='min(960,ih)':force_original_aspect_ratio=decrease",'-quality','86',str(poster)],check=True); record(source,poster,'poster','Video frame at 8s (Santa) or 1s (others), not a separate generated result.')
audio=out/'mother-song.mp3'
subprocess.run(['ffmpeg','-v','error','-y','-i',str(a.mother_video),'-vn','-c:a','libmp3lame','-b:a','128k',str(audio)],check=True)
record(a.mother_video,audio,'audio','Same soundtrack as approved den_mama.mp4; one-minute excerpt, not a full-length song example.')
if a.banners:
    for filename,name in [('c60aa946-daf8-4616-8945-19c0136ef7d8.png','postcard-birthday'),('035c843a-5992-42b4-8c4b-8dfe63cd409c.png','postcard-mother')]:
        picture(a.banners/filename,name)
if a.logo:
    logo_dir=out.parent/'logo'; logo_dir.mkdir(parents=True,exist_ok=True)
    im=ImageOps.exif_transpose(Image.open(a.logo)).convert('RGB')
    if im.width != im.height: raise ValueError('Original logo must be square; never stretch it.')
    copy=im.copy();copy.thumbnail((512,512));copy.save(logo_dir/'bot-logo.webp',quality=91,method=6)
    for size,name in [(32,'favicon-32.png'),(180,'apple-touch-icon.png')]: im.resize((size,size),Image.Resampling.LANCZOS).save(logo_dir/name,optimize=True)
    im.resize((64,64),Image.Resampling.LANCZOS).save(logo_dir/'favicon.ico',sizes=[(16,16),(32,32),(48,48),(64,64)])
    for file in [logo_dir/name for name in ('bot-logo.webp','favicon-32.png','apple-touch-icon.png','favicon.ico')]: manifest.append(dict(source=a.logo.name,file='/media/logo/'+file.name,kind='logo',sourceSha256=hashlib.sha256(a.logo.read_bytes()).hexdigest(),bytes=file.stat().st_size,approval='Owner supplied original bot logo for this website on 2026-11-26.',note='Square original resized only, no redraw or stretching.'))
for item in manifest:
    output=Path(__file__).resolve().parents[1]/'public'/item['file'].lstrip('/')
    item['bytes']=output.stat().st_size
    item['sha256']=hashlib.sha256(output.read_bytes()).hexdigest()
    manifest_by_file[item['file']]=item
registry.parent.mkdir(parents=True,exist_ok=True)
registry.write_text(json.dumps(sorted(manifest_by_file.values(),key=lambda x:x['file']),ensure_ascii=False,indent=2)+'\n')
print('Prepared',len(manifest),'approved assets, bytes',sum(x['bytes'] for x in manifest))

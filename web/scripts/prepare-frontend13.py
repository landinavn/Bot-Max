"""Offline Frontend 1.3 media preparation; exact sources.tsv allowlist only.

No approval is inferred from a filename or checksum. The owner must separately
confirm public-use rights before these privately prepared outputs are published.
Images retain their complete content and aspect ratio; no crops or redraws.

Usage: python prepare_frontend13.py --input /path/to/extracted --output /path/to/private-prepared
Requires Pillow, ffmpeg and ffprobe. Does not read configuration or use networks.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
from PIL import Image, ImageOps

IMAGE_NAMES = {
    'photoshoot-mother-daughter-elegant.png', 'photoshoot-family-light.png',
    'photoshoot-family-cozy.png', 'photoshoot-mother-daughter-red.png',
    'photoshoot-child-candy.webp', 'photoshoot-woman-red.png',
    'postcard-new-year-child-before-after.png', 'postcard-new-year-family-before-after.png',
    'postcard-new-year-mother-before-after.png', 'postcard-new-year-couple-before-after.png',
    'postcard-new-year-granddaughter.webp', 'postcard-birthday-friends-before-after.png',
    'vision-board-financial-dream.webp', 'vision-board-seaside-home.webp',
}
VIDEO_NAMES = {'animation-family-portrait-vertical.mp4', 'animation-family-portrait-horizontal.mp4'}
# Owner approval applies to this exact supplied source registry.
EXPECTED_SOURCES_SHA256 = '843c31b99c1e12c296fa93554ac2874f4080b9cdd3b646c135d233bdf0c4bbfa'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(path):
    return json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries',
        'stream=index,codec_type,codec_name,width,height,pix_fmt,sample_aspect_ratio:format=duration,size',
        '-of', 'json', str(path)], text=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root, out = args.input.resolve(), args.output.resolve()
    if root == out:
        raise ValueError('Keep input originals separate from derived output.')
    if sha(root/'sources.tsv') != EXPECTED_SOURCES_SHA256:
        raise ValueError('Source registry changed: recheck files and owner approval before publication.')
    with (root/'sources.tsv').open(newline='', encoding='utf-8-sig') as f:
        sources = list(csv.DictReader(f, delimiter='\t'))
    names = [r['archive_filename'] for r in sources]
    if len(names) != 16 or set(names) != IMAGE_NAMES | VIDEO_NAMES:
        raise ValueError('Expected exact 14-image / 2-video archive allowlist.')
    if len(set(names)) != len(names):
        raise ValueError('Duplicate source name.')
    for row in sources:
        source = root/row['archive_filename']
        if source.is_symlink() or source.parent != root or not source.is_file() or sha(source) != row['sha256']:
            raise ValueError('Source checksum/path mismatch: '+source.name)
    out.mkdir(parents=True, exist_ok=True)
    assets, entries = [], []

    def record(row, target, kind, *, dimensions=None, note='', extra=None):
        data = dict(file='/media/works/'+target.name, filename=target.name, kind=kind,
                    source=row['archive_filename'], originalFilename=row['original_filename'],
                    sourceSha256=row['sha256'], sha256=sha(target), bytes=target.stat().st_size,
                    approval='Owner explicitly confirmed public use of all 16 files, including identifiable adults and children, on 2026-11-26.', note=note)
        if dimensions:
            data.update(width=dimensions[0], height=dimensions[1])
        if extra:
            data.update(extra)
        assets.append(data)
        return data

    def images(row, image, kind='image', *, video_poster=False):
        image = ImageOps.exif_transpose(image).convert('RGB')
        original_size = image.size
        base_width = min(1400, image.width)
        sizes = [base_width, 480] if video_poster else [base_width, 900, 480]
        variants = []
        for width in sorted(set(min(w, image.width) for w in sizes), reverse=True):
            height = round(image.height * width / image.width)
            target = out/(Path(row['archive_filename']).stem + ('' if width == base_width else '-'+str(width)) + '.webp')
            resized = image.resize((width, height), Image.Resampling.LANCZOS) if width != image.width else image
            resized.save(target, 'WEBP', quality=88, method=6)
            # Existing native-size WebP is already compressed. Preserve its original
            # bytes rather than add an unnecessary lossy generation or extra size.
            source = root/row['archive_filename']
            if kind == 'image' and source.suffix.lower() == '.webp' and resized.size == original_size:
                shutil.copyfile(source, target)
            detail = 'Frame at 1.0s of the supplied video, not an original before photograph.' if video_poster else 'Full approved source composition resized only; no crop/redraw. Collages remain collages.'
            record(row, target, kind, dimensions=resized.size, note=detail,
                   extra={'frameTimeSeconds': 1} if video_poster else None)
            variants.append({'file':'/media/works/'+target.name, 'width':width, 'height':height})
        return {'poster': variants[0]['file'], 'width':variants[0]['width'],
                'height':variants[0]['height'], 'variants':variants,
                'srcSet':', '.join(v['file']+' '+str(v['width'])+'w' for v in sorted(variants,key=lambda v:v['width']))}

    for row in sources:
        source = root/row['archive_filename']
        if source.name in IMAGE_NAMES:
            with Image.open(source) as image:
                entry = dict(id=source.stem, kind='image', sourceWidth=image.width, sourceHeight=image.height,
                             **images(row,image))
            entries.append(entry)
            continue
        original = probe(source)
        original_video = next(s for s in original['streams'] if s['codec_type']=='video')
        target = out/source.name
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(source),'-map','0:v:0','-map','0:a?',
                        '-c:v','libx264','-preset','slow','-crf','22','-pix_fmt','yuv420p',
                        '-c:a','aac','-b:a','96k','-map_metadata','-1','-movflags','+faststart',str(target)],check=True)
        result = probe(target)
        video = next(s for s in result['streams'] if s['codec_type']=='video')
        if (video['width'], video['height']) != (original_video['width'],original_video['height']):
            raise ValueError('Video dimensions changed unexpectedly.')
        if abs(float(result['format']['duration'])-float(original['format']['duration'])) > .05:
            raise ValueError('Video duration changed unexpectedly.')
        record(row,target,'video',dimensions=(video['width'],video['height']),
               note='H.264 CRF22 slow / yuv420p / faststart; original dimensions, aspect and duration preserved. No crop.',
               extra={'durationSeconds':float(result['format']['duration']),'codec':'h264','sourceBytes':source.stat().st_size})
        frame = subprocess.check_output(['ffmpeg','-v','error','-ss','1','-i',str(source),'-frames:v','1','-f','image2pipe','-vcodec','png','-'])
        with Image.open(io.BytesIO(frame)) as image:
            poster = images(row,image,'poster',video_poster=True)
        entries.append(dict(id=source.stem,kind='video',src='/media/works/'+target.name,
                            durationSeconds=float(result['format']['duration']),**poster))
    (out/'media-metadata.json').write_text(json.dumps({'assets':assets,'entries':entries},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'files':len(assets),'images':14,'videos':2,'outputBytes':sum(a['bytes'] for a in assets)},indent=2))


if __name__ == '__main__':
    main()

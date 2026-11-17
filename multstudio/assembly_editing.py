"""Montage-only changes: source scene hashes and retained five-second clips stay intact."""
import json
import math


def trim_seconds(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=2:
        raise ValueError('Обрезка конца HeyGen должна быть от 0 до 2 секунд.')
    return float(value)


async def video_provider(db,scene):
    from . import repository as repo
    video=scene.get('assets',{}).get('video')
    if not video or not video.get('job_id'):return None
    job=await repo.one(db,'SELECT * FROM cartoon_jobs WHERE id=?',(video['job_id'],))
    if (not job or job['kind']!='video' or job['project_id']!=scene['project_id']
            or job['target']!='scene' or job['target_id']!=scene['id']):return None
    try:
        usage=json.loads(job['usage'] or '{}');snap=json.loads(job['snapshot'])
        return usage.get('provider') or snap.get('quote',{}).get('video_provider') or snap.get('spec',{}).get('video_provider')
    except (ValueError,TypeError,AttributeError):
        return None  # Unknown provenance is never guessed from the current scene settings.


async def attach(db,snapshot):
    amount=trim_seconds(snapshot['spec'].get('heygen_end_trim_seconds',0))
    if not amount:return
    for scene in snapshot['scenes']:
        provider=await video_provider(db,scene)
        if provider in ('heygen','heygen_video'):
            scene['assembly_editing']={'video_end_trim':amount,'source_video_provider':provider}

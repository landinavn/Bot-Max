"""Free scene-image import; no OpenAI or Kling calls."""
import asyncio
import io
import logging

from PIL import Image, ImageOps, UnidentifiedImageError

from . import repository as repo

logger = logging.getLogger(__name__)


def png_bytes(path):
    try:
        with Image.open(path) as image:
            if image.format not in ('JPEG', 'PNG') or image.width * image.height > 40_000_000:
                raise ValueError('Нужен JPG/PNG до 40 мегапикселей.')
            image.load()
            output = io.BytesIO()
            ImageOps.exif_transpose(image).convert('RGB').save(output, format='PNG')
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Не удалось прочитать изображение. Отправьте JPG/PNG.') from exc


async def upload_scene_image(assets, scene_id, actor, url):
    job_id, snap = await repo.begin_scene_upload(scene_id, actor)
    try:
        source, checksum = await assets.download(url, 'bin', limit=20*1024*1024,
                                                 job_id=job_id, name='source.bin')
        await repo.add_asset(snap['project_id'],'scene',scene_id,'scene_source',snap['context_hash'],
                             source,checksum,job_id=job_id,source_url=url)
        data = await asyncio.to_thread(png_bytes, assets.path(source))
        relative, checksum = await assets.write(data,'png',job_id=job_id,name='image.png')
        asset_id = await repo.add_asset(snap['project_id'],'scene',scene_id,'scene_upload_pending',
                                       snap['context_hash'],relative,checksum,job_id=job_id)
        async with repo.connect() as db:
            asset = await repo.one(db,'SELECT * FROM cartoon_assets WHERE id=?',(asset_id,))
        public_url = await assets.asset_url(asset)
        if not assets.public_image_url(public_url):
            raise ValueError('Публикация не вернула публичную ссылку Yandex Cloud на изображение.')
        await repo.finish_scene_upload(job_id, asset_id, public_url)
        logger.info('Изображение сцены загружено: scene=%s job=%s URL=%s',scene_id,job_id,public_url)
        return job_id, public_url
    except asyncio.CancelledError:
        await repo.update_job(job_id,status='upload_failed',error='interrupted',actual=0)
        raise
    except Exception as exc:
        await repo.update_job(job_id,status='upload_failed',error=type(exc).__name__,actual=0)
        logger.warning('Не завершена загрузка сцены %s (%s)',scene_id,type(exc).__name__)
        if isinstance(exc, ValueError):
            raise
        raise ValueError('Не удалось завершить загрузку изображения. Отправьте фото повторно; генерация не запускалась и плата не начислена.') from None

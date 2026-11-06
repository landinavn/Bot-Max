"""Free scene-image import; no OpenAI or Kling calls."""
import asyncio
import io
import logging

from PIL import Image, ImageOps, UnidentifiedImageError

from . import repository as repo

logger = logging.getLogger(__name__)


def png_bytes(path, *, job_id=None, scene_id=None):
    """Validate decoded content before conversion; names/MIME never grant access."""
    details = dict(job=job_id, scene=scene_id, bytes=path.stat().st_size)
    try:
        with Image.open(path) as image:
            details.update(format=image.format, width=image.width, height=image.height,
                           pixels=image.width * image.height, mode=image.mode)
            logger.info('Мультстудия: содержимое загруженного изображения %s',details)
            if image.format not in ('JPEG', 'PNG'):
                logger.warning('Мультстудия: отклонено изображение reason=unsupported_format %s',details)
                raise ValueError(
                    f'Скачанный файл имеет формат {image.format}, а не JPG/PNG. '
                    'MAX может преобразовать фото в WEBP. Отправьте исходный JPG/JPEG или PNG '
                    'через «Файл», а не как фото. Лимит — 40 000 000 пикселей.')
            if image.width * image.height > 40_000_000:
                logger.warning('Мультстудия: отклонено изображение reason=pixel_limit limit=40000000 %s',details)
                raise ValueError(f'Изображение {image.width} × {image.height} содержит '
                                 f'{image.width * image.height} пикселей. Лимит — 40 000 000 пикселей.')
            image.load()
            output = io.BytesIO()
            ImageOps.exif_transpose(image).convert('RGB').save(output, format='PNG')
            logger.info('Мультстудия: изображение проверено и преобразовано в PNG %s',details)
            return output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        logger.warning('Мультстудия: отклонено изображение reason=decompression_bomb limit=40000000 %s',details)
        raise ValueError('Разрешение изображения превышает допустимый лимит 40 000 000 пикселей.') from exc
    except (UnidentifiedImageError, OSError) as exc:
        logger.warning('Мультстудия: отклонено изображение reason=decode_failed error=%s %s',type(exc).__name__,details)
        raise ValueError('Не удалось прочитать изображение. Отправьте JPG/PNG.') from exc


async def upload_scene_image(assets, scene_id, actor, url):
    job_id, snap = await repo.begin_scene_upload(scene_id, actor)
    stage = 'download'
    try:
        logger.info('Мультстудия: скачивание исходника scene=%s job=%s limit_bytes=20971520',scene_id,job_id)
        source, checksum = await assets.download(url, 'bin', limit=20*1024*1024,
                                                 accept='image/jpeg, image/png',
                                                 job_id=job_id, name='source.bin')
        await repo.add_asset(snap['project_id'],'scene',scene_id,'scene_source',snap['context_hash'],
                             source,checksum,job_id=job_id,source_url=url)
        logger.info('Мультстудия: исходник сохранён scene=%s job=%s path=%s sha256=%s',scene_id,job_id,source,checksum)
        stage = 'validate_content'
        data = await asyncio.to_thread(png_bytes, assets.path(source),job_id=job_id,scene_id=scene_id)
        stage = 'save_png'
        relative, checksum = await assets.write(data,'png',job_id=job_id,name='image.png')
        logger.info('Мультстудия: PNG сохранён scene=%s job=%s path=%s bytes=%s',scene_id,job_id,relative,len(data))
        asset_id = await repo.add_asset(snap['project_id'],'scene',scene_id,'scene_upload_pending',
                                       snap['context_hash'],relative,checksum,job_id=job_id)
        async with repo.connect() as db:
            asset = await repo.one(db,'SELECT * FROM cartoon_assets WHERE id=?',(asset_id,))
        stage = 'publish'
        public_url = await assets.asset_url(asset)
        if not assets.public_image_url(public_url):
            raise ValueError('Публикация не вернула публичную ссылку Yandex Cloud на изображение.')
        stage = 'approve_scene'
        await repo.finish_scene_upload(job_id, asset_id, public_url)
        logger.info('Изображение сцены загружено: scene=%s job=%s URL=%s',scene_id,job_id,public_url)
        return job_id, public_url
    except asyncio.CancelledError:
        await repo.update_job(job_id,status='upload_failed',error='interrupted',actual=0)
        raise
    except Exception as exc:
        await repo.update_job(job_id,status='upload_failed',error=type(exc).__name__,actual=0)
        logger.warning('Не завершена загрузка сцены scene=%s job=%s stage=%s error=%s http_status=%s',
                       scene_id,job_id,stage,type(exc).__name__,getattr(getattr(exc,'response',None),'status_code',None))
        if isinstance(exc, ValueError):
            raise
        raise ValueError('Не удалось завершить загрузку изображения. Отправьте фото повторно; генерация не запускалась и плата не начислена.') from None

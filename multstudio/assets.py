"""Retained originals, immutable outputs and content hashes; no temporary-only results."""
import asyncio
import hashlib
import ipaddress
import socket
import logging
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from . import repository as repo
logger = logging.getLogger(__name__)


def https_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Нужна публичная HTTPS-ссылка без пароля.')
    return parsed


async def public_url(value):
    parsed = https_url(value)
    answers = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
        raise ValueError('Ссылки во внутреннюю сеть запрещены.')


class Assets:
    def __init__(self, root, config):
        self.root = Path(root).resolve()
        self.config = config

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('Некорректный путь файла.')
        return path

    def filename(self, suffix, job_id=None, name=None):
        if not re.fullmatch(r'[a-z0-9]+',suffix):
            raise ValueError('Некорректный формат файла.')
        name=name or uuid4().hex+'.'+suffix
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or name in ('.','..'):
            raise ValueError('Некорректное имя файла.')
        if job_id is not None:
            if not re.fullmatch(r'[a-f0-9]{32}',job_id):
                raise ValueError('Некорректный ID задания.')
            return job_id+'/'+name
        return name

    @staticmethod
    def checksum(path):
        result=hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda:source.read(1024*1024),b''):
                result.update(block)
        return result.hexdigest()

    def commit_file(self, temporary, destination, checksum):
        try:
            os.link(temporary,destination)  # Atomic create, never overwrite a previous version.
        except FileExistsError:
            if self.checksum(destination)!=checksum:
                raise ValueError('Файл этого задания уже существует с другим содержимым. Перегенерация требует нового задания.')
        finally:
            temporary.unlink(missing_ok=True)

    async def write(self, data, suffix, *, job_id=None, name=None):
        relative = self.filename(suffix,job_id,name)
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary=path.with_name('.part-'+uuid4().hex)
        checksum=hashlib.sha256(data).hexdigest()
        def save():
            try:
                temporary.write_bytes(data)
                self.commit_file(temporary,path,checksum)
            finally:
                temporary.unlink(missing_ok=True)
        await asyncio.to_thread(save)
        return relative,checksum

    async def download(self, url, suffix, limit=200*1024*1024, *, job_id=None, name=None):
        await public_url(url)
        self.root.mkdir(parents=True, exist_ok=True)
        relative = self.filename(suffix,job_id,name)
        path = self.path(relative)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name('.download-'+uuid4().hex)
        total, checksum = 0, hashlib.sha256()
        try:
            async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
                async with client.stream('GET', url) as response:
                    response.raise_for_status()
                    with temporary.open('xb') as output:
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > limit:
                                raise ValueError('Файл превышает лимит студии.')
                            checksum.update(chunk)
                            output.write(chunk)
            if not total:
                raise ValueError('Пустой файл.')
            await asyncio.to_thread(self.commit_file,temporary,path,checksum.hexdigest())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return relative, checksum.hexdigest()

    async def publish(self, relative):
        from image_service import _storage_client
        from urllib.parse import quote
        import mimetypes
        path = self.path(relative)
        config = self.config
        if not config.storage_bucket or not config.storage_public_base_url:
            raise ValueError('Настройте существующее Object Storage для файлов студии.')
        key = self.object_key(relative)
        image_url = self.canonical_image_url(key) if path.suffix.lower() in ('.png','.jpg','.jpeg','.webp') else None
        def upload():
            client = _storage_client(config)
            content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            if image_url:
                client.put_object(Bucket=config.storage_bucket,Key=key,Body=path.read_bytes(),ContentType=content_type)
            else:
                with path.open('rb') as source:
                    client.put_object(Bucket=config.storage_bucket,Key=key,Body=source,ContentType=content_type)
        await asyncio.to_thread(upload)
        if image_url:
            return image_url
        return config.storage_public_base_url.rstrip('/') + '/' + quote(key, safe='/')

    def object_key(self, relative):
        relative=self.path(relative).relative_to(self.root).as_posix()
        if re.match(r'^[a-f0-9]{32}/',relative):
            return 'studio/'+relative
        return relative if relative.startswith('studio/') else 'multstudio/'+relative

    def canonical_image_url(self, key):
        from urllib.parse import quote
        bucket=getattr(self.config,'storage_bucket',None)
        endpoint=urlsplit(getattr(self.config,'storage_endpoint_url','https://storage.yandexcloud.net'))
        if not bucket or endpoint.scheme!='https' or endpoint.hostname!='storage.yandexcloud.net':
            raise ValueError('Для изображений мультстудии требуется bucket в Yandex Cloud Object Storage.')
        return 'https://storage.yandexcloud.net/'+quote(bucket,safe='')+'/'+quote(key,safe='/')

    def public_image_url(self, url):
        if not isinstance(url,str): return False
        parsed=urlsplit(url)
        prefix=self.canonical_image_url('')
        return url.startswith(prefix) and bool(parsed.path[len(urlsplit(prefix).path):]) and not parsed.query and not parsed.fragment

    async def retain(self, snapshot, kind, data=None, url=None, suffix='png', job_id=None):
        filename=({'image':'image','character_reference':'image','video':'video','speech':'speech','assemble':'episode'}.get(kind)
                  or kind+'-'+uuid4().hex)+'.'+suffix if job_id else None
        options=dict(job_id=job_id,name=filename)
        relative, checksum = await self.download(url,suffix,**options) if url else await self.write(data,suffix,**options)
        return await repo.add_asset(snapshot['project_id'], snapshot['target'], snapshot['id'], kind,
            snapshot['context_hash'], relative, checksum, job_id=job_id, source_url=url)

    async def recover_image(self, asset):
        expected=asset.get('sha256','')
        if not re.fullmatch(r'[a-f0-9]{64}',expected):
            raise ValueError('Нет SHA-256 для безопасного восстановления изображения.')
        def locate():
            source=self.path(asset['path'])
            if source.is_file() and self.checksum(source)==expected:
                return source
            for candidate in self.root.rglob('*'):
                if candidate.suffix.lower() not in ('.png','.jpg','.jpeg','.webp') or not candidate.is_file():
                    continue
                candidate=self.path(candidate.relative_to(self.root))
                if self.checksum(candidate)==expected:
                    return candidate
            raise ValueError('Сохранённое изображение не найдено по SHA-256. Другой кадр не подставлен, генерация не запускалась.')
        source=await asyncio.to_thread(locate)
        new_path=source.relative_to(self.root).as_posix()
        if asset.get('job_id'):
            with source.open('rb') as file:
                header=file.read(3)
            suffix='jpg' if header==b'\xff\xd8\xff' else 'webp' if source.suffix.lower()=='.webp' else 'png'
            new_path=self.filename(suffix,asset['job_id'],'image.'+suffix)
            if source!=self.path(new_path):
                await self.write(await asyncio.to_thread(source.read_bytes),suffix,job_id=asset['job_id'],name='image.'+suffix)
        if new_path!=asset['path']:
            metadata=json.loads(asset.get('metadata') or '{}')
            metadata.setdefault('previous_paths',[]).append(asset['path'])
            metadata['recovered_from']=source.relative_to(self.root).as_posix()
            metadata.pop('published_key',None)
            async with repo.connect() as db:
                await db.execute('UPDATE cartoon_assets SET path=?,public_url=NULL,metadata=? WHERE id=?',
                                 (new_path,repo.encoded(metadata),asset['id']))
                await db.commit()
            asset=dict(asset,path=new_path,public_url=None,metadata=repo.encoded(metadata))
            logger.info('Восстановлен путь результата: job=%s path=%s',asset.get('job_id'),new_path)
        return asset

    async def asset_url(self, asset, *, force=False):
        if asset.get('id'):
            async with repo.connect() as db:
                stored=await repo.one(db,'SELECT * FROM cartoon_assets WHERE id=?',(asset['id'],))
            if not stored or stored['sha256']!=asset['sha256']:
                raise ValueError('Результат изменён или удалён из реестра.')
            asset=stored
        image=asset['kind'] in ('image','character_reference')
        if image:
            asset=await self.recover_image(asset)
        metadata=json.loads(asset.get('metadata') or '{}')
        key=self.object_key(asset['path'])
        verified=not image or (metadata.get('published_key')==key and metadata.get('published_sha256')==asset['sha256']
                              and asset.get('public_url')==self.canonical_image_url(key))
        if not force and asset.get('public_url') and verified:
            return asset['public_url']
        url = await self.publish(asset['path'])
        if image and url!=self.canonical_image_url(key):
            raise ValueError('Публикация не вернула публичную ссылку Yandex Cloud на изображение.')
        async with repo.connect() as db:
            metadata['published_key']=key
            metadata['published_sha256']=asset['sha256']
            await db.execute('UPDATE cartoon_assets SET public_url=?,metadata=? WHERE id=?', (url,repo.encoded(metadata),asset['id']))
            await db.commit()
        return url

    async def result_for_job(self, item, *, force=False):
        asset=await repo.job_result(item['id'])
        if not asset:
            raise ValueError('Файл результата этого задания не найден. Повторная генерация не запускалась.')
        url=await self.asset_url(asset,force=force)
        asset=await repo.job_result(item['id'])
        changed=await repo.bind_job_result(item['id'],asset['id'],url)
        if changed:
            logger.info('Результат сохранён: %s; job=%s',url,item['id'])
        return asset,url

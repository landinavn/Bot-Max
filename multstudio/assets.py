"""Retained originals, immutable outputs and content hashes; no temporary-only results."""
import asyncio
import hashlib
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from . import repository as repo


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

    async def write(self, data, suffix):
        relative = uuid4().hex + '.' + suffix
        path = self.path(relative)
        self.root.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return relative, hashlib.sha256(data).hexdigest()

    async def download(self, url, suffix, limit=200*1024*1024):
        await public_url(url)
        self.root.mkdir(parents=True, exist_ok=True)
        relative = uuid4().hex + '.' + suffix
        path = self.path(relative)
        total, checksum = 0, hashlib.sha256()
        try:
            async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
                async with client.stream('GET', url) as response:
                    response.raise_for_status()
                    with path.open('xb') as output:
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > limit:
                                raise ValueError('Файл превышает лимит студии.')
                            checksum.update(chunk)
                            output.write(chunk)
            if not total:
                raise ValueError('Пустой файл.')
        except BaseException:
            path.unlink(missing_ok=True)
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
        key = 'multstudio/' + path.name
        def upload():
            client = _storage_client(config)
            client.upload_file(str(path), config.storage_bucket, key,
                               ExtraArgs={'ContentType': mimetypes.guess_type(path.name)[0] or 'application/octet-stream'})
        await asyncio.to_thread(upload)
        return config.storage_public_base_url.rstrip('/') + '/' + quote(key, safe='/')

    async def retain(self, snapshot, kind, data=None, url=None, suffix='png', job_id=None):
        relative, checksum = await self.download(url, suffix) if url else await self.write(data, suffix)
        return await repo.add_asset(snapshot['project_id'], snapshot['target'], snapshot['id'], kind,
            snapshot['context_hash'], relative, checksum, job_id=job_id, source_url=url)

    async def asset_url(self, asset):
        if asset.get('public_url'):
            return asset['public_url']
        url = await self.publish(asset['path'])
        async with repo.connect() as db:
            await db.execute('UPDATE cartoon_assets SET public_url=? WHERE id=?', (url, asset['id']))
            await db.commit()
        return url

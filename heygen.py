"""HeyGen v3: one explicit mutation; durable IDs are owned by ded_moroz."""
import re
from urllib.parse import quote

import httpx

from multstudio.assets import https_url


class HeyGenError(RuntimeError):
    pass


class HeyGenAdapter:
    BASE = 'https://api.heygen.com'

    def __init__(self, key, transport=None):
        self.key, self.transport = key, transport

    async def request(self, method, path, *, payload=None, idempotency=None):
        if not self.key:
            raise ValueError('Задайте HEYGEN_API_KEY.')
        headers = {'X-Api-Key': self.key}
        if idempotency: headers['Idempotency-Key'] = idempotency
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False,
                                     transport=self.transport) as client:
            response = await client.request(method, self.BASE+path, headers=headers, json=payload)
        if response.status_code not in (200,201,202):
            # Never include keys, child facts or signed media URLs in logs/errors.
            raise HeyGenError(f'HeyGen HTTP {response.status_code}. Проверьте задание перед новым платным запросом.')
        result=response.json()
        data=result.get('data',result)
        if not isinstance(data,dict) or result.get('error'):
            raise HeyGenError('HeyGen вернул некорректный ответ.')
        return data

    @staticmethod
    def identifier(value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',value):
            raise HeyGenError('HeyGen не вернул корректный ID.')
        return value

    async def create_avatar(self, url, key):
        https_url(url)
        data=await self.request('POST','/v3/avatars',payload={
            'type':'photo','name':'Ded Moroz','file':{'type':'url','url':url}},idempotency=key)
        look=data.get('avatar_item') or {}
        group=data.get('avatar_group') or {}
        return dict(look_id=self.identifier(look.get('id') or data.get('look_id') or data.get('avatar_id')),
                    group_id=group.get('id'))

    async def avatar_status(self, look_id):
        data=await self.request('GET','/v3/avatars/looks/'+quote(self.identifier(look_id),safe=''))
        return data.get('avatar_item',data)

    async def create_video(self, avatar_id, audio_url, key):
        https_url(audio_url)
        data=await self.request('POST','/v3/videos',payload={
            'type':'avatar','avatar_id':self.identifier(avatar_id),'audio_url':audio_url,
            'aspect_ratio':'16:9','resolution':'1080p','engine':{'type':'avatar_iv'},
            'expressiveness':'medium'},idempotency=key)
        return self.identifier(data.get('video_id') or data.get('id'))

    async def video_status(self, video_id):
        data=await self.request('GET','/v3/videos/'+quote(self.identifier(video_id),safe=''))
        if data.get('status')=='completed':https_url(data.get('video_url',''))
        return data

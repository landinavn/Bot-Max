"""Studio-only use of the project's existing Photo Avatar / Avatar IV API contract."""
import httpx

from heygen import HeyGenAdapter, HeyGenError
from .adapters import TemporaryPollError, ProviderFailed
from .assets import https_url


class StudioHeyGen(HeyGenAdapter):
    def ready(self):
        if not self.key:
            raise ValueError('Задайте HEYGEN_API_KEY для говорящих сцен мультстудии.')

    async def create_scene_avatar(self, image_url, key):
        self.ready(); https_url(image_url)
        data=await self.request('POST','/v3/avatars',payload={
            'type':'photo','name':'Multstudio scene','file':{'type':'url','url':image_url}},idempotency=key)
        avatar=data.get('avatar_item') or {}
        return self.identifier(avatar.get('id') or data.get('look_id') or data.get('avatar_id'))

    async def scene_video(self, avatar_id, audio_url, key):
        self.ready(); https_url(audio_url)
        data=await self.request('POST','/v3/videos',payload={
            'type':'avatar','avatar_id':self.identifier(avatar_id),'audio_url':audio_url,
            'aspect_ratio':'9:16','resolution':'1080p','engine':{'type':'avatar_iv'},
            'expressiveness':'medium'},idempotency=key)
        return self.identifier(data.get('video_id') or data.get('id'))

    async def poll(self, provider_id, *, avatar=False):
        try:
            data=await (self.avatar_status(provider_id) if avatar else self.video_status(provider_id))
        except HeyGenError as exc:
            if exc.status_code==429 or (exc.status_code and exc.status_code>=500):
                raise TemporaryPollError('Временный сбой опроса HeyGen.') from None
            raise
        except httpx.RequestError:
            raise TemporaryPollError('Временный сбой соединения при опросе HeyGen.') from None
        if data.get('status') in ('failed','error'):
            raise ProviderFailed('HeyGen сообщил об ошибке сцены. Проверьте расход до повторной генерации.')
        if data.get('status')!='completed': return None
        if avatar: return dict(avatar_id=provider_id)
        https_url(data.get('video_url',''))
        return dict(url=data['video_url'])

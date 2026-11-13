"""Official Fish Audio REST integration, used only by Ded Moroz narration."""
import asyncio
import hashlib
import json
import logging
import math
import re
import shutil
from uuid import uuid4

import httpx

from elevenlabs_tts import clean_speech_text as strip_directions
from media_service import audio_duration

logger=logging.getLogger(__name__)
DEFAULT_VOICE_ID='fae6fa8fee9343049eecc1a1ffcb2acb'
DEFAULT_MODEL='s2.1-pro'


def prepare_speech_text(text):
    text=strip_directions(text)
    # Client facts sometimes start with "ты" after the template's existing "ты".
    return re.sub(r'\b(ты)(?:\s+ты)+\b',r'\1',text,flags=re.IGNORECASE)


class FishAudioError(RuntimeError):
    def __init__(self,code,message,*,status=None,safe_to_retry=False):
        super().__init__(message)
        self.code,self.status_code,self.safe_to_retry=code,status,safe_to_retry


class FishAudioAdapter:
    BASE='https://api.fish.audio'
    MAX_BYTES=30*1024*1024

    def __init__(self,key,voice_id,assets,*,model=DEFAULT_MODEL,transport=None):
        self.key,self.voice_id,self.assets,self.model,self.transport=key,voice_id,assets,model,transport

    @staticmethod
    def identifier(value):
        if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{32}',value):
            raise ValueError('Задайте FISH_AUDIO_VOICE_ID: 32-значный ID голоса Fish Audio.')
        return value

    @staticmethod
    def model_name(value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',value):
            raise ValueError('Задайте корректный FISH_AUDIO_MODEL.')
        return value

    def payload(self,text):
        text=prepare_speech_text(text)
        if not text or len(text)>10000:
            raise ValueError('Введите текст озвучки от 1 до 10000 символов после очистки.')
        self.model_name(self.model)
        return {'text':text,'reference_id':self.identifier(self.voice_id),
                'format':'mp3','sample_rate':44100,'mp3_bitrate':128,'language':'ru'}

    def fingerprint(self,text):
        data={'provider':'fish_audio','model':self.model,'request':self.payload(text)}
        return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

    def names(self,job_id,version):
        if not isinstance(version,int) or version<0:raise ValueError('Некорректная версия озвучки.')
        name=f'speech-v{version}.mp3';meta=f'speech-v{version}.fish.json'
        return self.assets.filename('mp3',job_id,name),self.assets.filename('json',job_id,meta)

    async def cached_speech(self,text,*,job_id,version):
        relative,metadata=self.names(job_id,version)
        path,manifest=self.assets.path(relative),self.assets.path(metadata)
        if not path.exists() and not manifest.exists():return None
        try:
            if not path.is_file() or not manifest.is_file() or path.stat().st_size>self.MAX_BYTES or manifest.stat().st_size>8192:
                raise ValueError
            data=json.loads(await asyncio.to_thread(manifest.read_text))
            checksum=await asyncio.to_thread(self.assets.checksum,path)
            if data['request_sha256']!=self.fingerprint(text) or data['audio_sha256']!=checksum:
                raise ValueError
            seconds=float(data['duration'])
            if not math.isfinite(seconds) or seconds<=0:raise ValueError
            await audio_duration(path)
        except (OSError,ValueError,KeyError,TypeError,RuntimeError):
            raise FishAudioError('cache_invalid','Сохранённая озвучка требует проверки файла и настроек.') from None
        return relative

    @staticmethod
    def response_error(status,*,voice_check=False):
        code,message={
            401:('authentication','Fish Audio: неверный или отсутствующий API-ключ.'),
            402:('balance','Fish Audio: недостаточно средств или доступа к платной модели.'),
            403:('forbidden','Fish Audio: доступ к голосу или API запрещён.'),
            404:('voice_unavailable','Fish Audio: выбранный голос не найден или недоступен.'),
            409:('voice_unavailable','Fish Audio: выбранный голос пока недоступен для синтеза.'),
            429:('rate_limit','Fish Audio: превышен лимит запросов. Повторите позже вручную.'),
        }.get(status,('invalid_request' if status in (400,422) else 'provider_error',
                     f'Fish Audio: запрос отклонён, HTTP {status}.'))
        logger.error('Fish Audio: %s, HTTP %s',code,status)
        return FishAudioError(code,message,status=status,
                              safe_to_retry=voice_check or status in (400,401,402,403,404,409,422,429))

    async def check_voice(self):
        """Read-only permission/readiness check; never substitute a different voice."""
        if not self.key:raise ValueError('Задайте FISH_AUDIO_API_KEY.')
        voice=self.identifier(self.voice_id)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20,connect=8),follow_redirects=False,
                                         trust_env=False,transport=self.transport) as client:
                response=await client.get(self.BASE+'/model/'+voice,
                                          headers={'Authorization':'Bearer '+self.key})
            if response.status_code!=200:raise self.response_error(response.status_code,voice_check=True)
            if len(response.content)>1024*1024:raise ValueError
            data=response.json()
            if not isinstance(data,dict) or data.get('_id')!=voice or data.get('type')!='tts':
                raise ValueError
            if data.get('state')!='trained' or data.get('dmca_taken_down'):
                raise FishAudioError('voice_unavailable','Fish Audio: выбранный голос не готов или снят с публикации.',safe_to_retry=True)
            return data
        except httpx.TimeoutException:
            raise FishAudioError('timeout','Fish Audio: таймаут проверки голоса.',safe_to_retry=True) from None
        except httpx.RequestError:
            raise FishAudioError('connection','Fish Audio: не удалось проверить доступ к голосу.',safe_to_retry=True) from None
        except (ValueError,TypeError):
            raise FishAudioError('voice_metadata','Fish Audio: некорректные сведения о выбранном голосе.',safe_to_retry=True) from None

    async def generate_speech(self,text: str,*,job_id=None,version=0) -> str:
        """One paid POST; return immutable MP3 path for the worker's S3 stage."""
        job_id=job_id or uuid4().hex
        payload=self.payload(text)
        cached=await self.cached_speech(text,job_id=job_id,version=version)
        if cached:return cached
        if not shutil.which('ffprobe'):raise FishAudioError('configuration','Для проверки MP3 установите ffprobe (пакет FFmpeg).',safe_to_retry=True)
        post_started=False
        try:
            async with asyncio.timeout(240):
                await self.check_voice()
                chunks=[];total=0
                headers={'Authorization':'Bearer '+self.key,'model':self.model,
                         'Content-Type':'application/json','Accept':'audio/mpeg'}
                async with httpx.AsyncClient(timeout=httpx.Timeout(180,connect=10,write=30,pool=10),
                                             follow_redirects=False,trust_env=False,transport=self.transport) as client:
                    post_started=True
                    async with client.stream('POST',self.BASE+'/v1/tts',headers=headers,json=payload) as response:
                        if response.status_code!=200:raise self.response_error(response.status_code)
                        if response.headers.get('content-type','').lower().split(';',1)[0] in ('application/json','text/html'):
                            raise FishAudioError('invalid_audio','Fish Audio не вернул MP3.')
                        async for chunk in response.aiter_bytes():
                            total+=len(chunk)
                            if total>self.MAX_BYTES:raise FishAudioError('invalid_audio','MP3 Fish Audio превышает 30 МиБ.')
                            chunks.append(chunk)
                if not total:raise FishAudioError('invalid_audio','Fish Audio вернул пустую озвучку.')
                name,meta=self.names(job_id,version)
                path,checksum=await self.assets.write(b''.join(chunks),'mp3',job_id=job_id,name=name.split('/')[-1])
                try:seconds=await audio_duration(self.assets.path(path))
                except RuntimeError:raise FishAudioError('invalid_audio','MP3 Fish Audio не прошёл проверку ffprobe.') from None
                manifest={'request_sha256':self.fingerprint(text),'audio_sha256':checksum,'duration':seconds}
                await self.assets.write(json.dumps(manifest).encode(),'json',job_id=job_id,name=meta.split('/')[-1])
                return path
        except (TimeoutError,httpx.TimeoutException):
            raise FishAudioError('timeout','Fish Audio: превышено время ожидания озвучки.',safe_to_retry=not post_started) from None
        except httpx.RequestError:
            raise FishAudioError('connection','Fish Audio: соединение прервано. Проверьте результат запроса.',safe_to_retry=not post_started) from None

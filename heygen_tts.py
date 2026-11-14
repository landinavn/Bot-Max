"""Ded Moroz's existing HeyGen Instant voice: durable URL and MP3, no paid retries."""
import asyncio
import hashlib
import json
import math
import re
import shutil
from uuid import uuid4

import httpx

from elevenlabs_tts import clean_speech_text as display_text
from heygen import HeyGenAdapter,HeyGenError
from media_service import audio_duration,run_media_process
from multstudio.assets import https_url

DEFAULT_VOICE_ID='3146577ad1e544c1a4658ded1090740c'
DEFAULT_MODEL='heygen-voice-1'
DEFAULT_SPEED=0.90
DEFAULT_ECHO_DELAY_MS=55
DEFAULT_ECHO_DECAY=0.10


def prepare_speech_text(text):
    if not isinstance(text,str):raise ValueError('Введите текст озвучки.')
    # Preserve supported HeyGen emotion tags and all Russian names.
    return re.sub(r'\b(ты)(?:\s+ты)+\b',r'\1',text.strip(),flags=re.IGNORECASE)


class HeyGenSpeechError(RuntimeError):
    def __init__(self,code,message,*,safe_to_retry=False):
        super().__init__(message);self.code,self.safe_to_retry=code,safe_to_retry


class HeyGenTTSAdapter:
    def __init__(self,key,voice_id,assets,*,model=DEFAULT_MODEL,transport=None,
                 speed=DEFAULT_SPEED,echo_delay_ms=DEFAULT_ECHO_DELAY_MS,echo_decay=DEFAULT_ECHO_DECAY):
        self.key,self.voice_id,self.assets,self.model=key,voice_id,assets,model
        self.client=HeyGenAdapter(key,transport,voice_id=voice_id,model=model)
        self.delivery=self.delivery_settings(speed,echo_delay_ms,echo_decay)

    @staticmethod
    def delivery_settings(speed,delay,decay):
        speed,delay,decay=map(float,(speed,delay,decay))
        if (not all(math.isfinite(v) for v in (speed,delay,decay))
            or not 0.5<=speed<=2 or not 0<=delay<=150 or not 0<=decay<=0.3):
            raise ValueError('Проверьте DED_MOROZ_SPEECH_SPEED (0.5–2), ECHO_DELAY_MS (0–150), ECHO_DECAY (0–0.3).')
        return {'version':'ded-voice-delivery-v1','speed':speed,'echo_delay_ms':delay,'echo_decay':decay}

    def delivery_hash(self):
        return hashlib.sha256(json.dumps(self.delivery,sort_keys=True).encode()).hexdigest()

    async def process_delivery(self,raw,output,meta,*,job_id,signature):
        # Dry signal 0.9 + 10% short delayed reflection: modest room echo, no long tail.
        filters=[f"atempo={self.delivery['speed']:.6f}"]
        if self.delivery['echo_delay_ms'] and self.delivery['echo_decay']:
            filters.append(f"aecho=0.9:1.0:{self.delivery['echo_delay_ms']:.3f}:{self.delivery['echo_decay']:.6f}")
        destination=self.assets.path(output)
        temporary=destination.with_name('.delivery-'+uuid4().hex+'.mp3')
        try:
            await run_media_process('ffmpeg','-nostdin','-v','error','-y','-protocol_whitelist','file,pipe',
                '-i',str(self.assets.path(raw)),'-vn','-af',','.join(filters),'-ar','44100','-ac','1',
                '-c:a','libmp3lame','-b:a','192k',str(temporary),timeout=120)
            if not temporary.stat().st_size or temporary.stat().st_size>30*1024*1024:
                raise HeyGenSpeechError('invalid_audio','Обработанная озвучка превышает допустимый размер.')
            seconds=await audio_duration(temporary)
            _,checksum=await self.assets.write(await asyncio.to_thread(temporary.read_bytes),'mp3',job_id=job_id,name=output.split('/')[-1])
            await self.assets.write(json.dumps({'request_sha256':signature,'audio_sha256':checksum,
                'duration':seconds,'delivery':self.delivery,'delivery_sha256':self.delivery_hash()}).encode(),
                'json',job_id=job_id,name=meta.split('/')[-1])
            return output
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def identifier(value):
        if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{32}',value):
            raise ValueError('Задайте DED_MOROZ_HEYGEN_VOICE_ID: ID вашего клона HeyGen.')
        return value

    @staticmethod
    def model_name(value):
        if value not in ('heygen-voice-1','heygen-voice-1-turbo','heygen-voice-1-turbo-free'):
            raise ValueError('Некорректный DED_MOROZ_TTS_MODEL.')
        return value

    def fingerprint(self,text):
        self.identifier(self.voice_id);self.model_name(self.model)
        body=self.client.speech_payload(prepare_speech_text(text))
        return hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def names(self,job_id,version):
        if not isinstance(version,int) or version<0:raise ValueError('Некорректная версия озвучки.')
        return tuple(self.assets.filename(ext,job_id,name) for ext,name in (
            ('mp3',f'speech-v{version}-delivery-{self.delivery_hash()[:12]}.mp3'),
            ('json',f'speech-v{version}-delivery-{self.delivery_hash()[:12]}.heygen.json'),
            ('json',f'speech-v{version}.heygen-source.json')))

    async def read_source(self,text,*,job_id,version):
        _,_,name=self.names(job_id,version);path=self.assets.path(name)
        if not path.exists():return None
        try:
            if path.stat().st_size>65536:raise ValueError
            data=json.loads(await asyncio.to_thread(path.read_text))
            if data['request_sha256']!=self.fingerprint(text):raise ValueError
            https_url(data['provider_url'])
            return data['provider_url']
        except (OSError,ValueError,KeyError,TypeError):
            raise HeyGenSpeechError('cache_invalid','Сохранённая ссылка озвучки требует проверки.') from None

    async def cached_speech(self,text,*,job_id,version):
        audio,manifest,_=self.names(job_id,version)
        path,meta=self.assets.path(audio),self.assets.path(manifest)
        if not meta.exists():return None
        try:
            if not path.is_file() or path.stat().st_size>30*1024*1024 or meta.stat().st_size>8192:raise ValueError
            data=json.loads(await asyncio.to_thread(meta.read_text))
            if data['request_sha256']!=self.fingerprint(text) or data.get('delivery_sha256')!=self.delivery_hash() or data['audio_sha256']!=await asyncio.to_thread(self.assets.checksum,path):raise ValueError
            await audio_duration(path)
        except (OSError,ValueError,KeyError,TypeError,RuntimeError):
            raise HeyGenSpeechError('cache_invalid','Сохранённый MP3 требует проверки файла и настроек.') from None
        return audio

    @staticmethod
    def error(exc,*,before_post=False):
        status=exc.status_code
        code,message={401:('authentication','Неверный API-ключ HeyGen.'),
            402:('balance','Недостаточно средств HeyGen.'),403:('forbidden','HeyGen запретил доступ к голосу.'),
            404:('voice_unavailable','Клон HeyGen не найден в этом workspace.'),
            409:('voice_unavailable','Клон HeyGen пока не готов.'),
            429:('rate_limit','Достигнут лимит HeyGen. Повторите позже вручную.')}.get(status,
            ('provider_error',f'Ошибка HeyGen{": HTTP "+str(status) if status else ": некорректный ответ"}.'))
        return HeyGenSpeechError(code,message,safe_to_retry=before_post or status in (400,401,402,403,404,409,422,429))

    async def check_voice(self):
        try:
            data=await self.client.request('GET','/v3/models/audio/voices/'+self.identifier(self.voice_id),timeout=20)
        except HeyGenError as exc:raise self.error(exc,before_post=True) from None
        except (ValueError,TypeError):raise HeyGenSpeechError('voice_unavailable','Некорректный ответ проверки клона HeyGen.',safe_to_retry=True) from None
        if data.get('voice_id')!=self.voice_id or data.get('status')!='ACTIVE' or data.get('mode')!='instant':
            raise HeyGenSpeechError('voice_unavailable','Выбранный Instant-клон HeyGen не готов или недоступен.',safe_to_retry=True)
        return data

    async def generate_speech(self,text,*,job_id=None,version=0):
        job_id=job_id or uuid4().hex;text=prepare_speech_text(text)
        signature=self.fingerprint(text)
        cached=await self.cached_speech(text,job_id=job_id,version=version)
        if cached:return cached
        audio,meta,source=self.names(job_id,version)
        url=await self.read_source(text,job_id=job_id,version=version)
        if self.assets.path(audio).exists() and not url:
            raise HeyGenSpeechError('cache_invalid','Существующий MP3 относится к прежней озвучке. Нужна новая версия.')
        if not shutil.which('ffprobe') or not shutil.which('ffmpeg'):raise HeyGenSpeechError('configuration','Установите FFmpeg и ffprobe.',safe_to_retry=True)
        post_started=False;source_saved=bool(url)
        try:
            async with asyncio.timeout(240):
                if not url:
                    await self.check_voice()
                    post_started=True
                    try:url=await self.client.generate_speech(text)
                    except HeyGenError as exc:raise self.error(exc) from None
                    # A saved URL proves synthesis completed: future retries only download.
                    await self.assets.write(json.dumps({'request_sha256':signature,'provider_url':url}).encode(),
                                            'json',job_id=job_id,name=source.split('/')[-1])
                    source_saved=True
                raw=self.assets.filename('mp3',job_id,f'speech-v{version}.raw.mp3')
                if not self.assets.path(raw).is_file():
                    await self.assets.download(url,'mp3',job_id=job_id,name=raw.split('/')[-1],limit=30*1024*1024)
                try:await audio_duration(self.assets.path(raw))
                except RuntimeError:raise HeyGenSpeechError('invalid_audio','MP3 HeyGen не прошёл проверку ffprobe.') from None
                return await self.process_delivery(raw,audio,meta,job_id=job_id,signature=signature)
        except (TimeoutError,httpx.TimeoutException):
            raise HeyGenSpeechError('timeout','Превышено время ожидания HeyGen.',safe_to_retry=not post_started or source_saved) from None
        except httpx.RequestError:
            raise HeyGenSpeechError('connection','Соединение с HeyGen прервано. Проверьте сохранённый результат.',safe_to_retry=not post_started or source_saved) from None

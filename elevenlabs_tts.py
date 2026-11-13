"""ElevenLabs narration for Ded Moroz; persist MP3 before the worker publishes it."""
import re
from urllib.parse import quote
from uuid import uuid4

import httpx


def clean_speech_text(text: str) -> str:
    """Remove bracketed directions, including nested/unfinished ones; preserve words."""
    if not isinstance(text,str):
        raise ValueError('Введите текст озвучки.')
    text=re.sub(r'\[\s*pause\s*\]', ' … ',text,flags=re.IGNORECASE)
    while re.search(r'\[[^\[\]]*\]',text):
        text=re.sub(r'\[[^\[\]]*\]', ' ',text)
    text=text.split('[',1)[0].replace(']','')
    text=re.sub(r'\s+',' ',text).strip()
    text=re.sub(r'\s+([,.;:!?…])',r'\1',text)
    text=re.sub(r'[,;:]\s*…','…',text)
    text=re.sub(r'([.!?])\s*…',r'\1',text)
    return text.strip()


class ElevenLabsError(RuntimeError):
    pass


class ElevenLabsAdapter:
    BASE='https://api.elevenlabs.io/v1/text-to-speech/'
    MAX_BYTES=30*1024*1024

    def __init__(self,key,voice_id,assets,transport=None):
        self.key,self.voice_id,self.assets,self.transport=key,voice_id,assets,transport

    @staticmethod
    def identifier(value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',value):
            raise ValueError('Задайте DED_MOROZ_VOICE_ID: ID голоса ElevenLabs.')
        return value

    async def generate_speech(self,text: str,*,job_id=None,version=0) -> str:
        """Return durable local MP3 path; the worker publishes its public audio_url.

        Keeping synthesis and publication separate makes S3 retries free of TTS calls.
        """
        text=clean_speech_text(text)
        if not text or len(text)>10000:
            raise ValueError('Введите текст озвучки от 1 до 10000 символов после очистки тегов.')
        if not self.key:raise ValueError('Задайте ELEVENLABS_API_KEY.')
        voice=self.identifier(self.voice_id)
        if not isinstance(version,int) or version<0:raise ValueError('Некорректная версия озвучки.')
        job_id=job_id or uuid4().hex
        name=f'speech-v{version}.mp3'
        relative=self.assets.filename('mp3',job_id,name)
        if self.assets.path(relative).exists():
            raise ValueError('MP3 этой версии уже сохранён. Проверьте заказ перед новым платным запросом.')
        headers={'xi-api-key':self.key,'Content-Type':'application/json','Accept':'audio/mpeg'}
        payload={'text':text,'model_id':'eleven_multilingual_v2',
                 'voice_settings':{'stability':0.5,'similarity_boost':0.75}}
        chunks=[];total=0
        async with httpx.AsyncClient(timeout=180,follow_redirects=False,trust_env=False,
                                     transport=self.transport) as client:
            async with client.stream('POST',self.BASE+quote(voice,safe=''),headers=headers,
                                     params={'output_format':'mp3_44100_128'},json=payload) as response:
                if response.status_code!=200:
                    raise ElevenLabsError(f'ElevenLabs TTS HTTP {response.status_code}. Проверьте запрос перед повторной генерацией.')
                if response.headers.get('content-type','').split(';',1)[0] in ('application/json','text/html'):
                    raise ElevenLabsError('ElevenLabs не вернул аудиофайл.')
                async for chunk in response.aiter_bytes():
                    total+=len(chunk)
                    if total>self.MAX_BYTES:raise ElevenLabsError('Аудиофайл ElevenLabs превышает 30 МиБ.')
                    chunks.append(chunk)
        if not total:raise ElevenLabsError('ElevenLabs вернул пустой аудиофайл.')
        path,_=await self.assets.write(b''.join(chunks),'mp3',job_id=job_id,name=name)
        return path

import asyncio
import time
import re
from uuid import uuid4

from accounts.models import normalize_email
from .email_sender import login_email
from .errors import DeliveryFailed, InvalidCode
from .models import CodeRequest
from .repository import AuthRepository
from .security import digest, generate_code


class AuthService:
    def __init__(self, path, settings, sender, *, clock=time.time, timeout=5):
        if getattr(sender,'test_only',False) and settings.environment!='test':
            raise ValueError('Тестовая почта запрещена вне тестового режима.')
        if not callable(getattr(sender,'send',None)):
            raise ValueError('Нужен явный email-адаптер.')
        self.settings,self.sender,self.clock=settings,sender,clock
        self.repository=AuthRepository(path,settings,timeout=timeout,clock=clock)

    def _source(self, source):
        # Future HTTP layer supplies a server-derived client key, never arbitrary X-Forwarded-For.
        if not isinstance(source,str) or not 1<=len(source)<=128 or not source.isascii() or any(ord(c)<32 or ord(c)==127 for c in source):
            raise ValueError('Некорректный источник запроса.')
        return digest(self.settings.secret,'source',source)

    async def request_email_code(self, email, *, source, browser_context=None):
        if not isinstance(email,str) or len(email)>512:raise ValueError('Некорректный email.')
        email=normalize_email(email); source_key=self._source(source)
        key=str(uuid4()); code=generate_code(self.settings.code_digits)
        email_key=digest(self.settings.secret,'email',email)
        mac=digest(self.settings.secret,'email-code',key,email,code)
        await self.repository.create_challenge(key,email,email_key,mac,source_key,int(self.clock()),browser_context=browser_context)
        state='unknown'
        try:
            await asyncio.wait_for(self.sender.send(login_email(email,code,key,self.settings)),self.settings.delivery_timeout)
            state='sent'
        except asyncio.CancelledError:
            await asyncio.shield(self.repository.mark_delivery(key,'unknown'))
            raise
        except DeliveryFailed:
            state='failed'
        except Exception:
            # Timeout/transport errors may hide successful acceptance. Never auto-resend or log contents.
            state='unknown'
        await self.repository.mark_delivery(key,state)
        return CodeRequest(key,self.settings.code_ttl)

    async def verify_email_code(self, challenge, code, *, source, browser_context=None):
        if not isinstance(code,str) or len(code)>128 or not isinstance(challenge,str) or len(challenge)>128:
            raise InvalidCode()
        if not re.fullmatch(r'[0-9]{'+str(self.settings.code_digits)+'}',code):
            code='invalid-code'
        return await self.repository.verify(challenge,code,self._source(source),int(self.clock()),browser_context=browser_context)

    async def get_current_account(self, token):
        return await self.repository.current(token,int(self.clock()))

    async def logout(self, token):
        await self.repository.logout(token,int(self.clock()))

    async def logout_all(self, token):
        # Ownership is derived from a valid session; no client-supplied account_id.
        await self.repository.logout(token,int(self.clock()),all_sessions=True)

    async def cleanup_expired(self):
        return await self.repository.cleanup(int(self.clock()))

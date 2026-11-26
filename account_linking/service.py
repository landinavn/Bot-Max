"""Internal application API. HTTP cannot submit MAX IDs or VerifiedIdentity objects."""
import time
from .repository import LinkingRepository
from .errors import InvalidLink
from .security import subject_mac


class LinkingService:
    def __init__(self,path,settings,*,clock=time.time,timeout=5):
        self.settings=settings
        self.repository=LinkingRepository(path,settings,clock=clock,timeout=timeout)

    def _source(self,value):
        if not isinstance(value,str) or not 1<=len(value)<=128 or not value.isascii() or any(ord(c)<32 or ord(c)==127 for c in value):
            raise InvalidLink()
        return subject_mac(self.settings.secret,'source',value)

    async def start(self,token,*,source):
        return await self.repository.start(token,self._source(source))

    async def status(self,token):
        return await self.repository.status(token)

    async def confirm(self,token,operation_id,*,source):
        return await self.repository.confirm(token,operation_id,self._source(source))

    async def cancel(self,token,operation_id,*,source):
        return await self.repository.cancel(token,operation_id,self._source(source))

    async def identities(self,token):
        return await self.repository.identities(token)

    # TRUSTED BOT ADAPTER ONLY: actor is obtained from authenticated private polling events.
    # These methods are never exposed through HTTP and never register a legacy user.
    async def registered_max(self,actor):
        return await self.repository.registered_max(actor)

    async def claim_from_max(self,actor,code):
        return await self.repository.claim(actor,code)

    async def confirm_from_max(self,actor,operation_id):
        return await self.repository.max_confirm(actor,operation_id)

    async def cancel_from_max(self,actor,operation_id):
        return await self.repository.max_cancel(actor,operation_id)

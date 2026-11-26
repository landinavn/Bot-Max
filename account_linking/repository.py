"""All transitions use one SQLite BEGIN IMMEDIATE; rejections commit rate counters."""
from datetime import datetime, timezone
from uuid import uuid4

from accounts import AccountRepository, VerifiedIdentity
from accounts.errors import LegacyMaxUserNotFound, LegacySchemaError, InvalidIdentity
from accounts.models import max_subject
from accounts.storage import connect, transaction
from auth.errors import InvalidSession
from auth.security import token_hash, challenge_id
from auth.repository import one
from . import security
from .errors import LinkingError, InvalidLink, FreshAuthRequired, LinkingLimited, LinkingUnavailable
from .models import Operation, Started, MaxClaim

LIVE = ('pending','max_confirmed')


class LinkingRepository:
    def __init__(self,path,settings,*,clock,timeout=5):
        self.accounts=AccountRepository(path,timeout=timeout)
        self.path,self.timeout=self.accounts.path,self.accounts.timeout
        self.settings,self.clock=settings,clock

    async def _write(self,action):
        async with connect(self.path,self.timeout) as db:
            async with transaction(db):
                result=await action(db,int(self.clock()))
        if isinstance(result,LinkingError):
            raise result
        return result

    async def _session(self,db,token,now,*,fresh=False):
        hashed=token_hash(token)
        if not hashed:
            raise InvalidSession()
        session=await one(db,"""SELECT s.* FROM auth_sessions s WHERE token_hash=? AND revoked_at IS NULL
            AND expires_at>? AND idle_expires_at>? AND created_at<=?""",(hashed,now,now,now))
        if not session:
            raise InvalidSession()
        email=await one(db,"SELECT id FROM account_identities WHERE account_id=? AND provider='email'",(session['account_id'],))
        if not email:
            raise InvalidSession()
        if fresh and now-session['created_at']>=self.settings.fresh_auth:
            raise FreshAuthRequired()
        return session

    async def _budget(self,db,rules,now):
        await db.execute('DELETE FROM account_link_limits WHERE created_at<=?',(now-self.settings.rate_window,))
        for event,subject,limit in rules:
            count=await one(db,'SELECT COUNT(*) AS n FROM account_link_limits WHERE event=? AND subject_mac=? AND created_at>?',
                            (event,subject,now-self.settings.rate_window))
            if count['n']>=limit:
                return False
        for event,subject,_ in rules:
            await db.execute('INSERT INTO account_link_limits VALUES(?,?,?,?)',(str(uuid4()),event,subject,now))
        return True

    async def _web_budget(self,db,session,source,now,*,start=False):
        s=self.settings;event='start' if start else 'action'
        account=security.subject_mac(s.secret,'account',session['account_id'])
        return await self._budget(db,[(event+'_account',account,s.account_starts if start else s.account_actions),
            (event+'_source',source,s.source_starts if start else s.source_actions),
            (event+'_global','global',s.global_starts if start else s.global_actions)],now)

    async def _max_budget(self,db,actor,now):
        s=self.settings
        return await self._budget(db,[('max_actor',security.subject_mac(s.secret,'max',actor),s.max_attempts),
            ('max_global','global',s.global_attempts)],now)

    async def _registered(self,db,actor):
        try:
            subject=max_subject(actor)
            await self.accounts._legacy_max(db,subject)
            return True
        except (LegacyMaxUserNotFound,LegacySchemaError,InvalidIdentity):
            return False

    async def registered_max(self,actor):
        async with connect(self.path,self.timeout,readonly=True) as db:
            return await self._registered(db,actor)

    @staticmethod
    def _view(row):
        return Operation(row['operation_id'],row['state'],row['expires_at'],row['max_confirmed_at'] is not None,
                         '…'+str(row['max_user_id'])[-4:] if row['max_confirmed_at'] is not None else None)

    async def _refresh(self,db,row,now):
        if row and row['state'] in LIVE and row['expires_at']<=now:
            await db.execute("UPDATE account_link_operations SET state='expired',invalidated_at=?,reason='timeout' WHERE operation_id=?",(now,row['operation_id']))
            row.update(state='expired',invalidated_at=now,reason='timeout')
        return row

    async def _owner(self,db,session,key,now):
        row=await one(db,'SELECT * FROM account_link_operations WHERE operation_id=? AND account_id=? AND session_id=?',
                      (challenge_id(key),session['account_id'],session['session_id']))
        return await self._refresh(db,row,now)

    async def _conflict(self,db,row):
        owner=await one(db,"SELECT account_id FROM account_identities WHERE provider='max' AND provider_subject=?",(str(row['max_user_id']),))
        existing=await one(db,"SELECT provider_subject FROM account_identities WHERE provider='max' AND account_id=?",(row['account_id'],))
        return bool((owner and owner['account_id']!=row['account_id']) or (existing and existing['provider_subject']!=str(row['max_user_id'])))

    async def _terminal(self,db,row,now,state,reason):
        await db.execute('UPDATE account_link_operations SET state=?,invalidated_at=?,reason=? WHERE operation_id=?',
                         (state,now,reason,row['operation_id']))
        row.update(state=state,invalidated_at=now,reason=reason)
        return self._view(row)

    async def start(self,token,source):
        async def action(db,now):
            session=await self._session(db,token,now,fresh=True)
            if not await self._web_budget(db,session,source,now,start=True):
                return LinkingLimited()
            for _ in range(8):
                code=security.new_code();mac=security.code_mac(self.settings.secret,code)
                if not await one(db,'SELECT operation_id FROM account_link_operations WHERE code_mac=?',(mac,)):
                    break
            else:
                return LinkingUnavailable()
            await db.execute("""UPDATE account_link_operations SET state='cancelled',invalidated_at=?,reason='superseded'
                WHERE account_id=? AND state IN ('pending','max_confirmed')""",(now,session['account_id']))
            key=str(uuid4());expires=now+self.settings.code_ttl
            await db.execute("""INSERT INTO account_link_operations(operation_id,account_id,session_id,code_mac,created_at,expires_at,state)
                VALUES(?,?,?,?,?,?,'pending')""",(key,session['account_id'],session['session_id'],mac,now,expires))
            return Started(Operation(key,'pending',expires,False),code)
        return await self._write(action)

    async def status(self,token):
        async def action(db,now):
            session=await self._session(db,token,now)
            row=await one(db,'SELECT * FROM account_link_operations WHERE account_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1',
                          (session['account_id'],session['session_id']))
            row=await self._refresh(db,row,now)
            return self._view(row) if row else None
        return await self._write(action)

    async def identities(self,token):
        async with connect(self.path,self.timeout,readonly=True) as db:
            session=await self._session(db,token,int(self.clock()))
            cursor=await db.execute('SELECT DISTINCT provider FROM account_identities WHERE account_id=? ORDER BY provider',(session['account_id'],))
            return [{'provider':r['provider'],'verified':True} for r in await cursor.fetchall()]

    async def claim(self,actor,code):
        async def action(db,now):
            if not await self._registered(db,actor):
                return InvalidLink()
            if not await self._max_budget(db,actor,now):
                return LinkingLimited()
            row=await one(db,'SELECT * FROM account_link_operations WHERE code_mac=?',(security.code_mac(self.settings.secret,code),))
            row=await self._refresh(db,row,now)
            if not row or row['state'] not in LIVE:
                return InvalidLink()
            if row['attempts']>=self.settings.operation_attempts:
                await self._terminal(db,row,now,'cancelled','attempt_limit')
                return InvalidLink()
            await db.execute('UPDATE account_link_operations SET attempts=attempts+1 WHERE operation_id=?',(row['operation_id'],))
            if row['max_user_id'] is not None and row['max_user_id']!=actor:
                return InvalidLink()
            # The first code claimant is pinned. Neither callbacks nor retries may replace them.
            if row['max_user_id'] is None:
                await db.execute('UPDATE account_link_operations SET max_user_id=?,max_claimed_at=? WHERE operation_id=?',(actor,now,row['operation_id']))
                row.update(max_user_id=actor,max_claimed_at=now)
            email=await one(db,"SELECT provider_subject FROM account_identities WHERE account_id=? AND provider='email' ORDER BY created_at LIMIT 1",(row['account_id'],))
            if not email:
                return InvalidLink()
            local,domain=email['provider_subject'].split('@')
            view=self._view(row)
            return MaxClaim(view.operation_id,view.status,view.expires_at,view.max_confirmed,view.max_hint,
                            local[0]+'***@'+domain[0]+'***')
        return await self._write(action)

    async def _max_action(self,actor,key,*,cancel=False):
        async def action(db,now):
            if not await self._registered(db,actor):
                return InvalidLink()
            if not await self._max_budget(db,actor,now):
                return LinkingLimited()
            row=await one(db,'SELECT * FROM account_link_operations WHERE operation_id=? AND max_user_id=?',(challenge_id(key),actor))
            row=await self._refresh(db,row,now)
            if not row:
                return InvalidLink()
            if row['state']=='completed':
                return self._view(row)
            if row['state'] not in LIVE:
                return InvalidLink()
            if cancel:
                return await self._terminal(db,row,now,'cancelled','max_cancelled')
            if await self._conflict(db,row):
                return await self._terminal(db,row,now,'conflict','identity_conflict')
            await db.execute("UPDATE account_link_operations SET state='max_confirmed',max_confirmed_at=COALESCE(max_confirmed_at,?) WHERE operation_id=?",(now,row['operation_id']))
            row.update(state='max_confirmed',max_confirmed_at=row['max_confirmed_at'] or now)
            return self._view(row)
        return await self._write(action)

    async def max_confirm(self,actor,key):
        return await self._max_action(actor,key)

    async def max_cancel(self,actor,key):
        return await self._max_action(actor,key,cancel=True)

    async def cancel(self,token,key,source):
        async def action(db,now):
            session=await self._session(db,token,now)
            if not await self._web_budget(db,session,source,now):
                return LinkingLimited()
            row=await self._owner(db,session,key,now)
            if not row:
                return InvalidLink()
            if row['state'] in LIVE:
                return await self._terminal(db,row,now,'cancelled','web_cancelled')
            return self._view(row)
        return await self._write(action)

    async def confirm(self,token,key,source):
        async def action(db,now):
            # Critical: validate session/freshness AFTER acquiring the write lock, not before it.
            session=await self._session(db,token,now,fresh=True)
            if not await self._web_budget(db,session,source,now):
                return LinkingLimited()
            row=await self._owner(db,session,key,now)
            if not row:
                return InvalidLink()
            if row['state']=='completed':
                return self._view(row)
            if row['state']!='max_confirmed' or row['max_confirmed_at'] is None or row['max_user_id'] is None:
                return InvalidLink()
            if not await self._registered(db,row['max_user_id']):
                return InvalidLink()
            if await self._conflict(db,row):
                return await self._terminal(db,row,now,'conflict','identity_conflict')
            proof=VerifiedIdentity('max',str(row['max_user_id']),datetime.fromtimestamp(row['max_confirmed_at'],timezone.utc))
            # Trusted insertion on THIS connection: no nested transaction or network.
            await self.accounts._insert_identity(db,row['account_id'],proof)
            await db.execute("UPDATE account_link_operations SET state='completed',completed_at=? WHERE operation_id=?",(now,row['operation_id']))
            row.update(state='completed',completed_at=now)
            return self._view(row)
        return await self._write(action)

"""Persistent limits and atomic code/account/session transitions on one SQLite connection."""
from datetime import datetime, timezone
import hmac
import math
import time
from uuid import uuid4

from accounts import AccountRepository, VerifiedIdentity
from accounts.models import Account
from accounts.storage import connect, transaction
from .errors import InvalidCode, InvalidSession, RateLimited
from .models import LoginResult
from .security import challenge_id, digest, generate_token, token_hash


async def one(db, sql, args=()):
    cursor = await db.execute(sql,args)
    row = await cursor.fetchone()
    return dict(row) if row else None


class AuthRepository:
    def __init__(self, path, settings, *, timeout=5, clock=time.time):
        self.accounts = AccountRepository(path,timeout=timeout)
        self.path, self.timeout = self.accounts.path, self.accounts.timeout
        self.settings = settings
        self.clock = clock

    async def _limit(self, db, event, key, now, limit):
        row = await one(db, '''SELECT COUNT(*) AS n FROM auth_rate_limits
            WHERE event=? AND subject_hash=? AND created_at>?''', (event,key,now-self.settings.rate_window))
        return row['n'] < limit

    async def _record(self, db, event, key, now):
        await db.execute('INSERT INTO auth_rate_limits VALUES(?,?,?,?)',(str(uuid4()),event,key,now))

    async def create_challenge(self, key, email, email_key, mac, source_key, now):
        s = self.settings
        async with connect(self.path,self.timeout) as db:
            async with transaction(db):
                now=int(self.clock())
                rules = [('request_email',email_key,s.email_requests),('request_source',source_key,s.source_requests),
                         ('request_global','global',s.global_requests)]
                last = await one(db,"SELECT MAX(created_at) AS t FROM auth_rate_limits WHERE event='request_email' AND subject_hash=?",(email_key,))
                if last['t'] is not None and now-last['t'] < s.resend_interval:
                    raise RateLimited()
                for event, subject, limit in rules:
                    if not await self._limit(db,event,subject,now,limit):raise RateLimited()
                for event, subject, _ in rules:await self._record(db,event,subject,now)
                await db.execute('''UPDATE auth_challenges SET invalidated_at=? WHERE email_key=?
                    AND consumed_at IS NULL AND invalidated_at IS NULL''',(now,email_key))
                await db.execute('''INSERT INTO auth_challenges(challenge_id,purpose,email,email_key,code_mac,created_at,expires_at,delivery_status)
                    VALUES(?,'email_login',?,?,?,?,?,'pending')''',(key,email,email_key,mac,now,now+s.code_ttl))

    async def mark_delivery(self, key, state):
        if state not in ('sent','failed','unknown'):raise ValueError('Некорректное состояние доставки.')
        async with connect(self.path,self.timeout) as db:
            async with transaction(db):
                now=int(self.clock())
                await db.execute("UPDATE auth_challenges SET delivery_status=? WHERE challenge_id=? AND delivery_status='pending'",(state,key))

    async def verify(self, key, code, source_key, now):
        s = self.settings
        error = None
        result = None
        async with connect(self.path,self.timeout) as db:
            async with transaction(db):
                now=int(self.clock())
                general = [('attempt_source',source_key,s.source_attempts),('attempt_global','global',s.global_attempts)]
                allowed = True
                for event,subject,limit in general:
                    if not await self._limit(db,event,subject,now,limit):allowed=False
                if not allowed:
                    error = RateLimited()
                else:
                    for event,subject,_ in general:await self._record(db,event,subject,now)
                    row = await one(db,'SELECT * FROM auth_challenges WHERE challenge_id=?',(challenge_id(key),))
                    if not row:
                        error = InvalidCode()
                    elif not await self._limit(db,'attempt_email',row['email_key'],now,s.email_attempts):
                        error = RateLimited()
                    else:
                        await self._record(db,'attempt_email',row['email_key'],now)
                        if row['delivery_status']=='pending' and now>=row['created_at']+math.ceil(s.delivery_timeout):
                            await db.execute("UPDATE auth_challenges SET delivery_status='unknown' WHERE challenge_id=?",(row['challenge_id'],))
                            row['delivery_status']='unknown'
                        live = (row['purpose']=='email_login' and row['expires_at']>now and row['consumed_at'] is None
                                and row['invalidated_at'] is None and row['locked_at'] is None
                                and row['delivery_status'] in ('sent','unknown') and row['attempts']<s.challenge_attempts)
                        expected = digest(s.secret,'email-code',row['challenge_id'],row['email'],code)
                        valid = hmac.compare_digest(expected,row['code_mac'])
                        if not live or not valid:
                            if live:
                                attempts=row['attempts']+1
                                await db.execute('UPDATE auth_challenges SET attempts=?,locked_at=? WHERE challenge_id=?',
                                                 (attempts,now if attempts>=s.challenge_attempts else None,row['challenge_id']))
                            error = InvalidCode()
                        else:
                            proof = VerifiedIdentity('email',row['email'],datetime.fromtimestamp(now,timezone.utc))
                            account = await self.accounts._get_or_create_verified_email_in_transaction(db,proof)
                            token=generate_token(); session=str(uuid4())
                            await self._insert_session(db,session,account.id,token,now)
                            await db.execute('UPDATE auth_challenges SET consumed_at=? WHERE challenge_id=?',(now,row['challenge_id']))
                            result=LoginResult(account,session,token,now+s.session_lifetime)
        # Rejections COMMIT attempt counters; raising inside the transaction would reset the budget.
        if error:raise error
        return result

    async def _insert_session(self, db, session, account, token, now):
        s=self.settings
        await db.execute('''INSERT INTO auth_sessions(session_id,account_id,token_hash,created_at,last_seen_at,expires_at,idle_expires_at)
            VALUES(?,?,?,?,?,?,?)''',(session,account,token_hash(token),now,now,now+s.session_lifetime,now+s.session_idle))

    async def current(self, token, now):
        hashed=token_hash(token)
        if not hashed:raise InvalidSession()
        sql='''SELECT s.*,a.created_at AS account_created,a.updated_at AS account_updated FROM auth_sessions s
            JOIN accounts a ON a.id=s.account_id WHERE s.token_hash=? AND s.revoked_at IS NULL
            AND s.expires_at>? AND s.idle_expires_at>?'''
        async with connect(self.path,self.timeout,readonly=True) as db:
            row=await one(db,sql,(hashed,now,now))
        now=int(self.clock())
        if not row or row['expires_at']<=now or row['idle_expires_at']<=now:raise InvalidSession()
        if now-row['last_seen_at']>=self.settings.session_touch:
            async with connect(self.path,self.timeout) as db:
                async with transaction(db):
                    now=int(self.clock())
                    row=await one(db,sql,(hashed,now,now))
                    if not row:raise InvalidSession()
                    if now-row['last_seen_at']>=self.settings.session_touch:
                        await db.execute('UPDATE auth_sessions SET last_seen_at=?,idle_expires_at=? WHERE session_id=?',
                            (now,now+self.settings.session_idle,row['session_id']))
        return Account(row['account_id'],row['account_created'],row['account_updated'])

    async def logout(self, token, now, *, all_sessions=False):
        hashed=token_hash(token)
        if not hashed:
            if all_sessions:raise InvalidSession()
            return
        async with connect(self.path,self.timeout) as db:
            async with transaction(db):
                now=int(self.clock())
                if all_sessions:
                    row=await one(db,'''SELECT account_id FROM auth_sessions WHERE token_hash=? AND revoked_at IS NULL
                        AND expires_at>? AND idle_expires_at>?''',(hashed,now,now))
                    if not row:raise InvalidSession()
                    await db.execute('UPDATE auth_sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL',(now,row['account_id']))
                else:
                    await db.execute('UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL',(now,hashed))

    async def cleanup(self, now):
        # Retain budgets for their full window even when the originating challenge is gone.
        async with connect(self.path,self.timeout) as db:
            async with transaction(db):
                now=int(self.clock())
                await db.execute('DELETE FROM auth_rate_limits WHERE created_at<=?',(now-self.settings.rate_window,))
                cursor=await db.execute('DELETE FROM auth_challenges WHERE expires_at<=?',(now-self.settings.retention,))
                count=cursor.rowcount
                await db.execute('DELETE FROM auth_sessions WHERE expires_at<=? OR (revoked_at IS NOT NULL AND revoked_at<=?)',
                                 (now-self.settings.retention,now-self.settings.retention))
                return count

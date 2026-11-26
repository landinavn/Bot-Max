"""Short BEGIN IMMEDIATE transactions; no process-local correctness locks."""
from dataclasses import asdict
import hashlib
import json
import time
from uuid import uuid4

from accounts.models import account_id
from accounts.storage import connect, connection_timeout, database_path, transaction
from .catalog import PRODUCTS, ORDER_SCOPED, SINGLE, BUCKETS, SOURCES
from .errors import Conflict, InsufficientRights, InvalidOperation, InvalidTransition, NotFound
from .models import (GrantSpec, VerifiedGrantEvent, VerifiedOutcome, Reservation, PackageView,
                     token, integer)


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


async def one(db, sql, args=()):
    c = await db.execute(sql, args)
    r = await c.fetchone()
    return dict(r) if r else None


async def rows(db, sql, args=()):
    c = await db.execute(sql, args)
    return [dict(r) for r in await c.fetchall()]


def validate_spec(spec):
    if (type(spec) is not GrantSpec or type(spec.kind) is not str or spec.kind not in PRODUCTS
            or type(spec.bucket) is not str or spec.bucket not in BUCKETS):
        raise InvalidOperation('Неизвестное право или источник.')
    integer(spec.quantity)
    if (type(spec.products) is not tuple or not spec.products or any(type(p) is not str for p in spec.products)
            or len(set(spec.products)) != len(spec.products)
            or not set(spec.products) <= PRODUCTS[spec.kind]):
        raise InvalidOperation('Несовместимое ограничение продукта.')
    token(spec.package_ref, optional=True)
    token(spec.order_ref, optional=True)
    if spec.kind in ORDER_SCOPED and (spec.order_ref is None or len(spec.products) != 1):
        raise InvalidOperation('Требуется отдельный заказ и один продукт.')
    if spec.kind in SINGLE and spec.quantity != 1:
        raise InvalidOperation('Заказ даёт одно право.')
    if spec.expires_at is not None: integer(spec.expires_at, minimum=0, maximum=2**63-1)


class EntitlementRepository:
    """Privileged internal API. Caller must authorize account and verify external evidence.

    Quantities are immutable lots. Availability is derived from durable allocations;
    no second mutable balance can diverge from the reservation journal.
    """
    def __init__(self, path, *, timeout=5, clock=None):
        self.path = database_path(path)
        self.timeout = connection_timeout(timeout)
        self.clock = clock or (lambda: int(time.time()))

    def now(self):
        return integer(self.clock(), minimum=0, maximum=2**63-1)

    async def _owner(self, db, owner):
        if not await one(db, 'SELECT id FROM accounts WHERE id=?', (owner,)):
            raise NotFound('Аккаунт не найден.')

    async def _previous(self, db, key, owner, action, payload):
        token(key)
        found = await one(db, 'SELECT * FROM entitlement_commands WHERE idempotency_key=?', (key,))
        if found and (found['account_id'], found['action'], found['payload_hash']) != (owner, action, digest(payload)):
            raise Conflict('Ключ уже использован для другой операции.')
        return found['result_id'] if found else None

    async def _remember(self, db, key, owner, action, payload, result):
        await db.execute('INSERT INTO entitlement_commands VALUES(?,?,?,?,?)',
                         (key, owner, action, digest(payload), result))

    async def _ledger(self, db, grant, operation, quantity, key, *, reservation=None,
                      parent=None, metadata=None):
        event = await one(db, 'SELECT * FROM entitlement_events WHERE id=?', (grant['event_id'],))
        op = str(uuid4())
        await db.execute('''INSERT INTO entitlement_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (op, grant['account_id'], grant['id'], reservation, grant['event_id'], grant['kind'],
             grant['package_ref'], grant['order_ref'], operation, quantity, event['source'],
             event['external_id'], key, self.now(), parent, encoded(metadata or {})))
        return op

    async def _new_grant(self, db, owner, event_id, spec, key, *, operation='grant', parent=None, metadata=None):
        if operation != 'compensate':
            duplicate = await one(db, '''SELECT id FROM entitlement_grants WHERE kind=?
                AND bucket!='compensation' AND ((? IS NOT NULL AND order_ref=?) OR (? IS NOT NULL AND package_ref=?))''',
                (spec.kind, spec.order_ref, spec.order_ref, spec.package_ref, spec.package_ref))
            if duplicate: raise Conflict('Пакет или заказ уже начислен.')
        gid = str(uuid4())
        await db.execute('INSERT INTO entitlement_grants VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (gid, owner, event_id, spec.kind, spec.bucket, encoded(sorted(spec.products)),
             spec.package_ref, spec.order_ref, spec.quantity, spec.expires_at, self.now()))
        grant = await one(db, 'SELECT * FROM entitlement_grants WHERE id=?', (gid,))
        await self._ledger(db, grant, operation, spec.quantity, key, parent=parent, metadata=metadata)
        return gid

    async def grant(self, event, *, idempotency_key):
        if type(event) is not VerifiedGrantEvent or type(event.source) is not str or event.source not in SOURCES:
            raise InvalidOperation('Нужно доверенное подтверждённое событие.')
        owner = account_id(event.account_id)
        token(event.external_id)
        integer(event.verified_at, minimum=0, maximum=2**63-1)
        if type(event.grants) is not tuple or not 1 <= len(event.grants) <= 50:
            raise InvalidOperation('Событие должно содержать права.')
        for spec in event.grants:
            validate_spec(spec)
            if spec.bucket == 'compensation':
                raise InvalidOperation('Компенсация требует исходного списания и метода compensate.')
        if type(event.metadata) is not tuple:
            raise InvalidOperation('Недопустимые метаданные.')
        for pair in event.metadata:
            if (type(pair) is not tuple or len(pair) != 2 or pair[0] not in
                    ('correlation_id', 'reason_code', 'policy_version')):
                raise InvalidOperation('Только технические метаданные.')
            token(pair[1])
        if len(dict(event.metadata)) != len(event.metadata): raise InvalidOperation('Повтор ключа метаданных.')
        payload = asdict(event)
        # Product order is semantic, not significant; event grant order is significant.
        for spec in payload['grants']: spec['products'] = sorted(spec['products'])
        async with connect(self.path, self.timeout) as db:
            async with transaction(db):
                await self._owner(db, owner)
                previous = await self._previous(db, idempotency_key, owner, 'grant', payload)
                if previous: return await self._event_grants(db, previous)
                found = await one(db, 'SELECT * FROM entitlement_events WHERE source=? AND external_id=?',
                                  (event.source, event.external_id))
                if found:
                    if found['account_id'] != owner or found['payload_hash'] != digest(payload):
                        raise Conflict('Событие уже учтено с другими параметрами.')
                    eid = found['id']
                else:
                    eid = str(uuid4())
                    await db.execute('INSERT INTO entitlement_events VALUES(?,?,?,?,?,?)',
                                     (eid, owner, event.source, event.external_id, digest(payload), event.verified_at))
                    for spec in event.grants:
                        await self._new_grant(db, owner, eid, spec, idempotency_key, metadata=dict(event.metadata))
                await self._remember(db, idempotency_key, owner, 'grant', payload, eid)
                return await self._event_grants(db, eid)

    async def _event_grants(self, db, eid):
        return tuple(r['id'] for r in await rows(db, 'SELECT id FROM entitlement_grants WHERE event_id=? ORDER BY rowid', (eid,)))

    async def _lots(self, db, owner):
        return await rows(db, '''SELECT g.*,
            COALESCE(SUM(CASE WHEN r.state IN ('reserved','submitting','uncertain') THEN a.quantity ELSE 0 END),0) AS reserved,
            COALESCE(SUM(CASE WHEN r.state='committed' THEN a.quantity ELSE 0 END),0) AS consumed
            FROM entitlement_grants g LEFT JOIN entitlement_allocations a ON a.grant_id=g.id
            LEFT JOIN entitlement_reservations r ON r.id=a.reservation_id
            WHERE g.account_id=? GROUP BY g.id ORDER BY g.expires_at IS NULL,g.expires_at,g.created_at,g.id''', (owner,))

    def _eligible(self, grant, kind, product, order_ref, now):
        return (grant['kind'] == kind and product in json.loads(grant['products'])
                and grant['order_ref'] == order_ref
                and (grant['expires_at'] is None or grant['expires_at'] > now))

    def _request(self, kind, product, order_ref):
        if type(kind) is not str or kind not in PRODUCTS or type(product) is not str or product not in PRODUCTS[kind]:
            raise InvalidOperation('Несовместимый продукт.')
        token(order_ref, optional=True)
        if kind in ORDER_SCOPED and order_ref is None: raise InvalidOperation('Нужен идентификатор заказа.')

    async def reserve(self, owner, kind, product, quantity, *, generation_ref, idempotency_key, order_ref=None):
        owner = account_id(owner)
        self._request(kind, product, order_ref)
        integer(quantity)
        if kind in SINGLE and quantity != 1: raise InvalidOperation('Один заказ — одно право.')
        token(generation_ref)
        payload = dict(kind=kind, product=product, quantity=quantity, order_ref=order_ref, generation_ref=generation_ref)
        async with connect(self.path, self.timeout) as db:
            async with transaction(db):
                await self._owner(db, owner)
                previous = await self._previous(db, idempotency_key, owner, 'reserve', payload)
                if previous: return await self._reservation(db, owner, previous)
                found = await one(db, 'SELECT * FROM entitlement_reservations WHERE account_id=? AND generation_ref=?',
                                  (owner, generation_ref))
                if found:
                    if any(found[k] != v for k, v in payload.items()):
                        raise Conflict('Задание уже связано с другим резервом.')
                    await self._remember(db, idempotency_key, owner, 'reserve', payload, found['id'])
                    return Reservation(**found)
                now = self.now()
                lots = [g for g in await self._lots(db, owner) if self._eligible(g, kind, product, order_ref, now)]
                if sum(g['quantity']-g['reserved']-g['consumed'] for g in lots) < quantity:
                    raise InsufficientRights('Недостаточно доступных прав.')
                rid = str(uuid4())
                await db.execute('INSERT INTO entitlement_reservations VALUES(?,?,?,?,?,?,?,?,?)',
                                 (rid, owner, kind, product, quantity, order_ref, generation_ref, 'reserved', now))
                remaining = quantity
                for g in lots:
                    take = min(remaining, g['quantity']-g['reserved']-g['consumed'])
                    if take:
                        await db.execute('INSERT INTO entitlement_allocations VALUES(?,?,?)', (rid, g['id'], take))
                        await self._ledger(db, g, 'reserve', take, idempotency_key, reservation=rid)
                        remaining -= take
                    if not remaining: break
                await self._remember(db, idempotency_key, owner, 'reserve', payload, rid)
                return await self._reservation(db, owner, rid)

    async def _reservation(self, db, owner, rid):
        found = await one(db, 'SELECT * FROM entitlement_reservations WHERE id=? AND account_id=?', (rid, owner))
        if not found: raise NotFound('Резерв не найден.')
        return Reservation(**found)

    async def get_reservation(self, owner, reservation_id):
        owner = account_id(owner)
        token(reservation_id)
        async with connect(self.path, self.timeout, readonly=True) as db:
            return await self._reservation(db, owner, reservation_id)

    async def _transition(self, owner, rid, target, key, verification=None):
        owner = account_id(owner)
        token(rid)
        if target in ('committed', 'released'):
            allowed = ('success',) if target == 'committed' else ('failed', 'not_submitted')
            if type(verification) is not VerifiedOutcome or verification.outcome not in allowed:
                raise InvalidOperation('Нужно подтверждение результата.')
            token(verification.evidence_ref)
        elif verification is not None: raise InvalidOperation('Недопустимое подтверждение.')
        action = {'committed': 'commit', 'released': 'release'}.get(target, target)
        payload = dict(reservation_id=rid, target=target, evidence=asdict(verification) if verification else None)
        async with connect(self.path, self.timeout) as db:
            async with transaction(db):
                previous = await self._previous(db, key, owner, action, payload)
                if previous: return await self._reservation(db, owner, previous)
                current = await self._reservation(db, owner, rid)
                if current.state != target:
                    if (current.state in ('committed', 'released') or
                        (target == 'submitting' and current.state != 'reserved') or
                        (verification and verification.outcome == 'not_submitted' and current.state != 'reserved')):
                        raise InvalidTransition('Результат не допускает эту операцию; резерв сохранён.')
                    await db.execute('UPDATE entitlement_reservations SET state=? WHERE id=?', (target, rid))
                    for a in await rows(db, 'SELECT * FROM entitlement_allocations WHERE reservation_id=?', (rid,)):
                        g = await one(db, 'SELECT * FROM entitlement_grants WHERE id=?', (a['grant_id'],))
                        parent = await one(db, "SELECT operation_id FROM entitlement_ledger WHERE reservation_id=? AND grant_id=? AND operation='reserve'", (rid, g['id']))
                        await self._ledger(db, g, action, a['quantity'], key, reservation=rid,
                                           parent=parent['operation_id'], metadata=payload['evidence'])
                await self._remember(db, key, owner, action, payload, rid)
                return await self._reservation(db, owner, rid)

    async def commit(self, owner, reservation_id, *, verification, idempotency_key):
        return await self._transition(owner, reservation_id, 'committed', idempotency_key, verification)

    async def release(self, owner, reservation_id, *, verification, idempotency_key):
        return await self._transition(owner, reservation_id, 'released', idempotency_key, verification)

    async def mark_submitting(self, owner, reservation_id, *, idempotency_key):
        return await self._transition(owner, reservation_id, 'submitting', idempotency_key)

    async def mark_uncertain(self, owner, reservation_id, *, idempotency_key):
        return await self._transition(owner, reservation_id, 'uncertain', idempotency_key)

    async def get_available(self, owner, kind, product, *, order_ref=None):
        owner = account_id(owner)
        self._request(kind, product, order_ref)
        async with connect(self.path, self.timeout, readonly=True) as db:
            await self._owner(db, owner)
            now = self.now()
            return sum(g['quantity']-g['reserved']-g['consumed'] for g in await self._lots(db, owner)
                       if self._eligible(g, kind, product, order_ref, now))

    async def list_packages(self, owner):
        owner = account_id(owner)
        async with connect(self.path, self.timeout, readonly=True) as db:
            await self._owner(db, owner)
            return self._views(await self._lots(db, owner), self.now())

    def _views(self, lots, now):
        result = []
        for g in lots:
            rest = g['quantity']-g['reserved']-g['consumed']
            expired = g['expires_at'] is not None and g['expires_at'] <= now
            state = 'held' if g['reserved'] else ('expired' if expired else ('exhausted' if not rest else 'available'))
            result.append(PackageView(g['id'], g['kind'], tuple(json.loads(g['products'])), g['bucket'],
                                      0 if expired else rest, g['reserved'], g['consumed'], rest if expired else 0,
                                      state, g['expires_at']))
        return tuple(result)

    async def compensate(self, owner, operation_id, quantity, *, evidence_ref, expires_at, idempotency_key):
        """Privileged support-only correction, not a cash refund. Explicit expiry required.

        Bound to an original commit; total compensation cannot exceed its debit.
        A new immutable lot is created, preserving original consumption/history.
        """
        owner = account_id(owner)
        token(operation_id)
        token(evidence_ref)
        integer(quantity)
        if expires_at is not None: integer(expires_at, minimum=0, maximum=2**63-1)
        payload = dict(operation_id=operation_id, quantity=quantity, evidence_ref=evidence_ref, expires_at=expires_at)
        async with connect(self.path, self.timeout) as db:
            async with transaction(db):
                previous = await self._previous(db, idempotency_key, owner, 'compensate', payload)
                if previous: return previous
                original = await one(db, "SELECT * FROM entitlement_ledger WHERE operation_id=? AND account_id=? AND operation='commit'", (operation_id, owner))
                if not original: raise NotFound('Подтверждённое списание не найдено.')
                external = await one(db, "SELECT * FROM entitlement_events WHERE source='support' AND external_id=?", (evidence_ref,))
                if external:
                    if external['account_id'] != owner or external['payload_hash'] != digest(payload):
                        raise Conflict('Подтверждение уже использовано иначе.')
                    gid = (await self._event_grants(db, external['id']))[0]
                else:
                    compensated = await one(db, "SELECT COALESCE(SUM(quantity),0) AS n FROM entitlement_ledger WHERE parent_operation_id=? AND operation='compensate'", (operation_id,))
                    if quantity+compensated['n'] > original['quantity']: raise Conflict('Превышено исходное списание.')
                    g = await one(db, 'SELECT * FROM entitlement_grants WHERE id=?', (original['grant_id'],))
                    spec = GrantSpec(g['kind'], quantity, tuple(json.loads(g['products'])), 'compensation',
                                     g['package_ref'], g['order_ref'], expires_at)
                    validate_spec(spec)
                    eid = str(uuid4())
                    await db.execute('INSERT INTO entitlement_events VALUES(?,?,?,?,?,?)', (eid, owner, 'support', evidence_ref, digest(payload), self.now()))
                    gid = await self._new_grant(db, owner, eid, spec, idempotency_key, operation='compensate', parent=operation_id)
                await self._remember(db, idempotency_key, owner, 'compensate', payload, gid)
                return gid

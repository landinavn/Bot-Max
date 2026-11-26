"""Read-only legacy interpretation through an existing, verified account/MAX link.

No SELECT *; no media, prompts, FSM payloads, personal details or payment links.
Numbers here describe legacy only, and must never be added to new ledger balances.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from accounts.models import account_id, max_subject
from accounts.errors import InvalidIdentity
from accounts.storage import connect, connection_timeout, database_path
from .errors import LegacySchemaError, UnverifiedLink
from .repository import one, rows


@dataclass(frozen=True)
class LegacyRight:
    table: str
    reference: str
    kind: str
    bucket: str
    status: str
    available: int | None
    reserved: int | None
    consumed: int | None
    provider_ref: str | None = None  # internal reconciliation only, never future public DTO
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegacySnapshot:
    account_id: str
    rights: tuple[LegacyRight, ...]
    observations: tuple[str, ...]
    trial_granted: bool
    tables: tuple[str, ...]


# Audited sizes, NOT prices. IDs 590/990/1490 are historical tariff keys.
PHOTO_SIZES = {590: 3, 990: 5, 1490: 7}
ANIMATION_SIZES = {'intro': 1, 'one': 1, 'three': 3, 'five': 5, 'promo1111_bonus': 1}
PAYMENTS = ('payment_orders', 'photo_orders', 'audio_orders', 'musical_orders',
            'cartoon_orders', 'animation_orders', 'ded_moroz_payments')
EXCLUDED = ('pending_videos', 'active_promos', 'promo_redemptions', 'promo_offers',
            'payment_incidents', 'payment_recoveries', 'mother_admin_sessions',
            'cartoon_avatar_archive', 'shared_images', 'ded_moroz_audio_versions',
            'ded_moroz_mobile_jobs')


def count(value):
    if type(value) is not int or not 0 <= value <= 10**9:
        raise LegacySchemaError('Некорректное целое значение legacy.')
    return value


def flag(value):
    if count(value) not in (0, 1): raise LegacySchemaError('Некорректный флаг legacy.')
    return value


def reference(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
        raise LegacySchemaError('Некорректная техническая ссылка legacy.')
    return value


class LegacyReader:
    def __init__(self, path, *, timeout=5):
        self.path = database_path(path)
        self.timeout = connection_timeout(timeout)

    async def read(self, owner):
        owner = account_id(owner)
        async with connect(self.path, self.timeout, readonly=True) as db:
            await db.execute('PRAGMA query_only=ON')
            await db.execute('BEGIN')
            return await self._read(db, owner)

    async def _columns(self, db, table, required):
        obj = await one(db, "SELECT type FROM sqlite_master WHERE name=?", (table,))
        if not obj or obj['type'] != 'table': raise LegacySchemaError('Отсутствует ожидаемая legacy-таблица: '+table)
        cols = {r['name']: r for r in await rows(db, 'PRAGMA table_info('+table+')')}
        if not set(required) <= cols.keys(): raise LegacySchemaError('Неизвестная структура legacy: '+table)
        int_columns = {'user_id','balance','free_trial_used','promo_image_balance','free_animation_count',
            'amount_kopecks','credits','quantity','slot','delivered','submit_started','running','failed',
            'free_access','regeneration_used','input_ready','prepaid','is_admin','promo_bonus','children_count'}
        for name in required:
            declared = cols[name]['type'].upper()
            expected_int = name in int_columns or (name == 'tariff' and table == 'photo_orders')
            allowed = {'INTEGER'} if expected_int else {'TEXT'}
            if table == 'users' and name == 'free_trial_used': allowed.add('BOOLEAN')
            if name == 'paid_at': allowed.add('TIMESTAMP')
            if declared not in allowed:
                raise LegacySchemaError('Неизвестный тип столбца legacy: '+table+'.'+name)
        return cols

    async def _payment_rows(self, db, table, extra):
        cols = ('order_id', 'user_id', 'status', 'amount_kopecks', 'provider_payment_id', 'paid_at', *extra)
        schema = await self._columns(db, table, cols)
        if not schema['order_id']['pk'] or sum(bool(c['pk']) for c in schema.values()) != 1:
            raise LegacySchemaError('Неизвестный ключ legacy-заказа: '+table)
        return ','.join(cols)

    def _paid(self, row, table, *, statuses=('paid',)):
        issues = []
        reference(row['order_id'])
        if row['provider_payment_id'] is not None: reference(row['provider_payment_id'])
        count(row['amount_kopecks'])
        if row['status'] not in statuses:
            issues.append('unknown_status' if row['status'] != 'pending' else 'unconfirmed_payment')
            return False, issues
        if not row['provider_payment_id'] or not row['paid_at'] or row['amount_kopecks'] == 0:
            issues.append('missing_payment_evidence_or_non_cash_grant')
        if row['paid_at']:
            try: datetime.fromisoformat(row['paid_at'].replace('Z', '+00:00'))
            except (ValueError, TypeError, AttributeError): issues.append('invalid_payment_timestamp')
        return not issues, issues

    async def _read(self, db, owner):
        identity = await one(db, """SELECT i.provider_subject,i.verified_at FROM account_identities i JOIN accounts a ON a.id=i.account_id
            WHERE i.account_id=? AND i.provider='max'""", (owner,))
        if not identity or not identity['verified_at']: raise UnverifiedLink('Нужна ранее подтверждённая связь MAX.')
        try:
            verified = datetime.fromisoformat(identity['verified_at'].replace('Z', '+00:00'))
            if verified.tzinfo is None or verified > datetime.now(timezone.utc): raise ValueError()
            subject = identity['provider_subject']
            if max_subject(int(subject)) != subject: raise ValueError()
            uid = int(subject)
        except (ValueError, TypeError, InvalidIdentity): raise UnverifiedLink('Некорректная связь MAX.') from None
        cols = await self._columns(db, 'users', ('user_id', 'balance', 'free_trial_used', 'promo_image_balance', 'free_animation_count'))
        if cols['user_id']['type'].upper() != 'INTEGER' or not cols['user_id']['pk']:
            raise LegacySchemaError('Неизвестный ключ users.')
        user = await one(db, 'SELECT balance,free_trial_used,promo_image_balance,free_animation_count FROM users WHERE user_id=?', (uid,))
        if not user: raise UnverifiedLink('Legacy-пользователь не существует.')
        trial = bool(flag(user['free_trial_used']))
        found = [LegacyRight('users', 'shared-image-wallet', 'image_credit', 'mixed', 'current',
                             count(user['balance']), None, None, issues=('mixed_paid_trial_bonus_origin', 'no_durable_image_reservation')),
                 LegacyRight('users', 'image-campaign-wallet', 'image_credit', 'promo', 'current',
                             count(user['promo_image_balance']), None, None, issues=('postcard_and_vision_board_only',)),
                 LegacyRight('users', 'animation-bonus-wallet', 'animation', 'bonus', 'current',
                             count(user['free_animation_count']), 0, None, issues=('materialized_bonus_orders_counted_separately',))]
        observations = ['trial_granted_no_second_trial' if trial else 'trial_not_granted_not_automatically_eligible']
        tables = tuple(r['name'] for r in await rows(db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
        # A partial/unrecognized deployment is refused, rather than reporting zero rights.
        extra = {
            'payment_orders': ('credits',),
            'photo_orders': ('tariff', 'generation_status'),
            'audio_orders': ('audio_format', 'submit_started', 'job_id', 'running', 'delivered', 'failed', 'free_access', 'regeneration_used'),
            'musical_orders': ('tariff', 'delivered', 'running', 'failed', 'input_ready', 'prepaid'),
            'cartoon_orders': ('tariff', 'theme'),
            'animation_orders': ('tariff', 'quantity', 'is_admin', 'promo_bonus'),
            'ded_moroz_payments': ('claimed_id', 'variant', 'children_count'),
        }
        for table in PAYMENTS:
            names = await self._payment_rows(db, table, extra[table])
            for r in await rows(db, 'SELECT '+names+' FROM '+table+' WHERE user_id=? ORDER BY order_id', (uid,)):
                paid, issues = self._paid(r, table, statuses=('paid', 'completed', 'manual') if table == 'cartoon_orders' else ('paid',))
                provider = r['provider_payment_id']
                noncash_animation = (table == 'animation_orders' and r['status']=='paid'
                    and r['amount_kopecks']==0 and not provider and (r['promo_bonus']==1 or r['is_admin']==1))
                if noncash_animation:
                    paid = True
                    issues = ['non_cash_bonus_or_admin_order']
                if not paid:
                    # Noncash/admin/granted-without-receipt and unknown rows are not silently paid rights.
                    found.append(LegacyRight(table, r['order_id'], 'evidence', 'unknown', str(r['status']), None, None, None,
                                             provider, tuple(issues)))
                    continue
                available = reserved = consumed = None
                kind = 'evidence'
                bucket = 'bonus' if noncash_animation else 'paid'
                state = r['status']
                if table == 'payment_orders':
                    count(r['credits'])
                    issues.append('provenance_only_already_in_wallet')
                elif table == 'photo_orders':
                    kind = 'photo_frame'
                    size = PHOTO_SIZES.get(r['tariff'])
                    if size is None: issues.append('unknown_tariff')
                    await self._columns(db, 'photo_results', ('order_id', 'slot', 'delivered'))
                    results = await rows(db, 'SELECT slot,delivered FROM photo_results WHERE order_id=?', (r['order_id'],))
                    slots = [count(x['slot']) for x in results]
                    for x in results: flag(x['delivered'])
                    if size is not None and (len(set(slots)) != len(slots) or any(not 1 <= x <= size for x in slots)):
                        raise LegacySchemaError('Несогласованные слоты фотосессии.')
                    state = r['generation_status']
                    if size is not None:
                        consumed = len(results)  # Saved, NOT merely delivered.
                        if state == 'completed' and consumed != size: issues.append('completed_with_missing_results')
                        if state in ('queued', 'completed'):
                            available = size-consumed
                            # Legacy has no durable barrier around each image POST.
                            if available: issues.append('remaining_slots_may_be_inflight_no_durable_reservation')
                        else: issues.append('unknown_generation_status')
                elif table == 'animation_orders':
                    kind = 'animation'
                    size = count(r['quantity'])
                    flag(r['is_admin']); flag(r['promo_bonus'])
                    if not size or ANIMATION_SIZES.get(r['tariff']) != size: issues.append('unknown_tariff_or_quantity')
                    await self._columns(db, 'animation_jobs', ('order_id', 'user_id', 'slot', 'status', 'task_id'))
                    jobs = await rows(db, 'SELECT user_id,slot,status,task_id FROM animation_jobs WHERE order_id=?', (r['order_id'],))
                    slots = [count(j['slot']) for j in jobs]
                    if len(set(slots)) != len(slots) or any(not 1 <= s <= size for s in slots) or any(j['user_id'] != uid for j in jobs):
                        raise LegacySchemaError('Несогласованные слоты оживления.')
                    if any(j['status'] not in ('queued','creating','processing','ready','delivered','manual') for j in jobs):
                        issues.append('unknown_generation_status')
                    if any(j['status']=='creating' and not j['task_id'] for j in jobs): issues.append('unknown_provider_outcome')
                    if any(j['status']=='manual' for j in jobs): issues.append('manual_review_retains_slot')
                    if not any(x.startswith('unknown_') for x in issues):
                        available = size-len(jobs)
                    consumed = sum(j['status']=='delivered' for j in jobs)
                    reserved = len(jobs)-consumed
                    if r['is_admin'] or r['promo_bonus']: issues.append('non_cash_provenance_needs_review')
                elif table == 'audio_orders':
                    kind = 'audio_order'
                    for k in ('submit_started','running','delivered','failed','free_access','regeneration_used'): flag(r[k])
                    if r['audio_format'] not in ('fun','gift'): issues.append('unknown_tariff')
                    if r['delivered']: available,reserved,consumed = 0,0,1
                    else:
                        reserved = 1
                        issues.append('unknown_provider_outcome' if r['submit_started'] and not r['job_id'] else 'unfinished_order_no_transfer')
                    if r['failed']: issues.append('manual_review')
                    if r['free_access']: issues.append('admin_free_access')
                    if r['delivered'] and not r['regeneration_used']: issues.append('included_regeneration_policy_not_transferable')
                elif table == 'musical_orders':
                    kind = 'musical_order'
                    for k in ('delivered','running','failed','input_ready','prepaid'): flag(r[k])
                    if r['tariff'] not in ('direct','upsell'): issues.append('unknown_tariff')
                    if r['delivered']: available,reserved,consumed = 0,0,1
                    else: reserved = 1; issues.append('unfinished_nested_submission_state_not_read')
                    if r['failed']: issues.append('manual_review')
                elif table == 'cartoon_orders':
                    kind = 'cartoon_order'
                    if r['tariff'] not in ('mother','mother_bday','short','long'): issues.append('unknown_tariff')
                    if r['status']=='completed': available,reserved,consumed = 0,0,1
                    else:
                        reserved = 1
                        issues.append('manual_review' if r['status']=='manual' else 'unfinished_nested_submission_state_not_read')
                elif table == 'ded_moroz_payments':
                    kind = 'ded_moroz_order'
                    if r['variant'] not in ('single','family'): issues.append('unknown_tariff')
                    if r['variant']=='family' and (type(r['children_count']) is not int or not 2 <= r['children_count'] <= 6):
                        issues.append('invalid_family_size')
                    if not r['claimed_id']:
                        issues.append('paid_but_missing_claim')
                    else:
                        await self._columns(db, 'ded_moroz_orders', ('id','user_id','status','payment_order_id'))
                        claim = await one(db, 'SELECT user_id,status,payment_order_id FROM ded_moroz_orders WHERE id=?', (r['claimed_id'],))
                        if not claim or claim['user_id'] != uid or claim['payment_order_id'] != r['order_id']:
                            raise LegacySchemaError('Несогласованная привязка поздравления.')
                        state = claim['status']
                        if state=='done': available,reserved,consumed = 0,0,1
                        elif state in ('input','audio_queued','audio_publish','audio_ready','editing','video_queued','polling','download','render'):
                            reserved=1; issues.append('claimed_service_not_unused_credit')
                        else: issues.append('manual_review' if state in ('manual','cancelled') else 'unknown_generation_status')
                found.append(LegacyRight(table, r['order_id'], kind, bucket, state, available, reserved, consumed, provider, tuple(issues)))
        for table in EXCLUDED:
            if table in tables: observations.append(table+':not_an_additional_grant')
        # Recoveries, rejected receipts, broadcasts, assets and campaigns are deliberately
        # not turned into units. A future import must explicitly review provenance.
        return LegacySnapshot(owner, tuple(found), tuple(observations), trial, tables)

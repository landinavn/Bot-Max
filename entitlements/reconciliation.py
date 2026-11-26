"""Dry-run only, consistent read snapshot; deliberately no import/apply method."""
from dataclasses import dataclass
from accounts.models import account_id
from accounts.storage import connect
from .legacy_reader import LegacyReader, LegacySnapshot
from .models import PackageView
from .repository import EntitlementRepository, rows


@dataclass(frozen=True)
class DryRunReport:
    legacy: LegacySnapshot
    new_packages: tuple[PackageView, ...]
    possible_duplicates: tuple[str, ...]
    ambiguities: tuple[str, ...]
    unknown_states: tuple[str, ...]
    discrepancies: tuple[str, ...]
    transfer_plan: tuple[str, ...]
    execution_allowed: bool = False


async def reconcile(path, owner, *, timeout=5, clock=None):
    owner = account_id(owner)
    reader = LegacyReader(path, timeout=timeout)
    repo = EntitlementRepository(path, timeout=timeout, clock=clock)
    async with connect(path, timeout, readonly=True) as db:
        await db.execute('PRAGMA query_only=ON')
        await db.execute('BEGIN')
        legacy = await reader._read(db, owner)
        packages = repo._views(await repo._lots(db, owner), repo.now())
        events = await rows(db, 'SELECT source,external_id FROM entitlement_events WHERE account_id=?', (owner,))
        new_refs = {e['external_id'] for e in events if e['source']=='payment.prodamus'}
        by_provider = {}
        duplicates, ambiguities, unknown, discrepancies = [], [], [], []
        for right in legacy.rights:
            ref = right.table+':'+right.reference
            if right.provider_ref and right.status != 'pending':
                if right.provider_ref in by_provider: duplicates.append(ref+':legacy_payment_reused')
                by_provider[right.provider_ref] = ref
                if right.provider_ref in new_refs: duplicates.append(ref+':legacy_and_new_event')
            for issue in right.issues:
                ambiguities.append(ref+':'+issue)
                if issue.startswith('unknown_'): unknown.append(ref+':'+issue)
                if issue.startswith(('completed_with_', 'invalid_', 'missing_payment', 'paid_but_')):
                    discrepancies.append(ref+':'+issue)
        if packages and legacy.rights:
            ambiguities.append('coexisting_sources_never_sum_or_enable_dual_spend')
        plan = tuple('BLOCKED_MANUAL_REVIEW:'+r.table+':'+r.reference for r in legacy.rights
                     if r.status != 'pending')
        return DryRunReport(legacy, packages, tuple(duplicates), tuple(ambiguities), tuple(unknown),
                            tuple(discrepancies), plan)

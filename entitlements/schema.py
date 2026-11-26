"""v5: explicit additive migration only. No legacy table changes."""
NAME = 'entitlements_ledger_and_reservations'
DDL = {
    'entitlement_events': '''CREATE TABLE entitlement_events (
        id TEXT PRIMARY KEY NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
        source TEXT NOT NULL, external_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
        verified_at INTEGER NOT NULL CHECK(typeof(verified_at)='integer' AND verified_at>=0),
        UNIQUE(source,external_id)
    )''',
    'entitlement_grants': '''CREATE TABLE entitlement_grants (
        id TEXT PRIMARY KEY NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
        event_id TEXT NOT NULL REFERENCES entitlement_events(id) ON DELETE RESTRICT,
        kind TEXT NOT NULL, bucket TEXT NOT NULL, products TEXT NOT NULL,
        package_ref TEXT, order_ref TEXT,
        quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0 AND quantity<=1000000000),
        expires_at INTEGER CHECK(expires_at IS NULL OR (typeof(expires_at)='integer' AND expires_at>=0)),
        created_at INTEGER NOT NULL
    )''',
    'entitlement_reservations': '''CREATE TABLE entitlement_reservations (
        id TEXT PRIMARY KEY NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
        kind TEXT NOT NULL, product TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0 AND quantity<=1000000000),
        order_ref TEXT, generation_ref TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved','submitting','uncertain','committed','released')),
        created_at INTEGER NOT NULL,
        UNIQUE(account_id,generation_ref)
    )''',
    'entitlement_allocations': '''CREATE TABLE entitlement_allocations (
        reservation_id TEXT NOT NULL REFERENCES entitlement_reservations(id) ON DELETE RESTRICT,
        grant_id TEXT NOT NULL REFERENCES entitlement_grants(id) ON DELETE RESTRICT,
        quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
        PRIMARY KEY(reservation_id,grant_id)
    )''',
    'entitlement_commands': '''CREATE TABLE entitlement_commands (
        idempotency_key TEXT PRIMARY KEY NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
        action TEXT NOT NULL, payload_hash TEXT NOT NULL, result_id TEXT NOT NULL
    )''',
    'entitlement_ledger': '''CREATE TABLE entitlement_ledger (
        operation_id TEXT PRIMARY KEY NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
        grant_id TEXT NOT NULL REFERENCES entitlement_grants(id) ON DELETE RESTRICT,
        reservation_id TEXT REFERENCES entitlement_reservations(id) ON DELETE RESTRICT,
        event_id TEXT NOT NULL REFERENCES entitlement_events(id) ON DELETE RESTRICT,
        kind TEXT NOT NULL, package_ref TEXT, order_ref TEXT,
        operation TEXT NOT NULL CHECK(operation IN ('grant','reserve','submitting','uncertain','commit','release','compensate')),
        quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>=0),
        source TEXT NOT NULL, external_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, created_at INTEGER NOT NULL,
        parent_operation_id TEXT REFERENCES entitlement_ledger(operation_id) ON DELETE RESTRICT,
        metadata TEXT NOT NULL
    )''',
    'entitlement_grant_owner': 'CREATE INDEX entitlement_grant_owner ON entitlement_grants(kind,order_ref)',
    'entitlement_reservation_owner': 'CREATE INDEX entitlement_reservation_owner ON entitlement_reservations(account_id,state)',
    'entitlement_ledger_owner': 'CREATE INDEX entitlement_ledger_owner ON entitlement_ledger(account_id,created_at)',
}
DDL['entitlement_unique_order'] = "CREATE UNIQUE INDEX entitlement_unique_order ON entitlement_grants(kind,order_ref) WHERE order_ref IS NOT NULL AND bucket!='compensation'"
DDL['entitlement_unique_package'] = "CREATE UNIQUE INDEX entitlement_unique_package ON entitlement_grants(kind,package_ref) WHERE package_ref IS NOT NULL AND bucket!='compensation'"
# Register every protective trigger with the migration validator/checksum.
for table in ('entitlement_events', 'entitlement_grants', 'entitlement_allocations',
              'entitlement_commands', 'entitlement_ledger'):
    for verb in ('UPDATE', 'DELETE'):
        name = table + '_no_' + verb.lower()
        DDL[name] = f"CREATE TRIGGER {name} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'append_only'); END"
DDL['entitlement_reservation_no_delete'] = """CREATE TRIGGER entitlement_reservation_no_delete
    BEFORE DELETE ON entitlement_reservations BEGIN SELECT RAISE(ABORT,'append_only'); END"""
DDL['entitlement_reservation_transition'] = """CREATE TRIGGER entitlement_reservation_transition
    BEFORE UPDATE ON entitlement_reservations WHEN
        NEW.id IS NOT OLD.id OR NEW.account_id IS NOT OLD.account_id OR NEW.kind IS NOT OLD.kind
        OR NEW.product IS NOT OLD.product OR NEW.quantity IS NOT OLD.quantity
        OR NEW.order_ref IS NOT OLD.order_ref OR NEW.generation_ref IS NOT OLD.generation_ref
        OR NEW.created_at IS NOT OLD.created_at OR NOT (
            (OLD.state='reserved' AND NEW.state IN ('submitting','uncertain','committed','released')) OR
            (OLD.state='submitting' AND NEW.state IN ('uncertain','committed','released')) OR
            (OLD.state='uncertain' AND NEW.state IN ('committed','released')))
    BEGIN SELECT RAISE(ABORT,'invalid_transition'); END"""
DDL['entitlement_allocation_guard'] = """CREATE TRIGGER entitlement_allocation_guard
    BEFORE INSERT ON entitlement_allocations WHEN
        NOT EXISTS (SELECT 1 FROM entitlement_grants g JOIN entitlement_reservations r
            ON r.id=NEW.reservation_id WHERE g.id=NEW.grant_id AND g.account_id=r.account_id
            AND g.kind=r.kind AND g.order_ref IS r.order_ref AND r.state='reserved')
        OR NEW.quantity + COALESCE((SELECT SUM(a.quantity) FROM entitlement_allocations a
            JOIN entitlement_reservations r ON r.id=a.reservation_id
            WHERE a.grant_id=NEW.grant_id AND r.state!='released'),0)
            > (SELECT quantity FROM entitlement_grants WHERE id=NEW.grant_id)
        OR NEW.quantity + COALESCE((SELECT SUM(quantity) FROM entitlement_allocations
            WHERE reservation_id=NEW.reservation_id),0)
            > (SELECT quantity FROM entitlement_reservations WHERE id=NEW.reservation_id)
    BEGIN SELECT RAISE(ABORT,'invalid_allocation'); END"""

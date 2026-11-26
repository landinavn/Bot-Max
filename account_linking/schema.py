"""Additive v4. No alterations to immutable v1/v2/v3 DDL."""
NAME = 'two_sided_max_linking'
DDL = {
    'account_link_operations': '''CREATE TABLE account_link_operations (
        operation_id TEXT NOT NULL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
        session_id TEXT NOT NULL,
        code_mac TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        max_user_id INTEGER,
        max_claimed_at INTEGER,
        max_confirmed_at INTEGER,
        completed_at INTEGER,
        invalidated_at INTEGER,
        reason TEXT,
        state TEXT NOT NULL CHECK(state IN ('pending','max_confirmed','completed','cancelled','expired','conflict'))
    )''',
    'account_link_owner': 'CREATE INDEX account_link_owner ON account_link_operations(account_id,session_id,created_at)',
    'account_link_expiry': 'CREATE INDEX account_link_expiry ON account_link_operations(state,expires_at)',
    'account_link_limits': '''CREATE TABLE account_link_limits (
        id TEXT NOT NULL PRIMARY KEY,
        event TEXT NOT NULL,
        subject_mac TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )''',
    'account_link_limit_window': 'CREATE INDEX account_link_limit_window ON account_link_limits(event,subject_mac,created_at)',
}

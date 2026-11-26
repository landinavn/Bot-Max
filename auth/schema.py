"""Immutable migration v2. No imports that read config or open a connection."""
NAME = 'email_challenges_and_sessions'
DDL = {
    'auth_challenges': '''CREATE TABLE auth_challenges (
        challenge_id TEXT NOT NULL PRIMARY KEY,
        purpose TEXT NOT NULL CHECK(purpose='email_login'),
        email TEXT NOT NULL,
        email_key TEXT NOT NULL,
        code_mac TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        consumed_at INTEGER,
        invalidated_at INTEGER,
        locked_at INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        delivery_status TEXT NOT NULL CHECK(delivery_status IN ('pending','sent','failed','unknown'))
    )''',
    'auth_challenge_email': 'CREATE INDEX auth_challenge_email ON auth_challenges(email_key,created_at)',
    'auth_challenge_expiry': 'CREATE INDEX auth_challenge_expiry ON auth_challenges(expires_at)',
    'auth_sessions': '''CREATE TABLE auth_sessions (
        session_id TEXT NOT NULL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
        token_hash TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        idle_expires_at INTEGER NOT NULL,
        revoked_at INTEGER
    )''',
    'auth_session_owner': 'CREATE INDEX auth_session_owner ON auth_sessions(account_id)',
    'auth_session_expiry': 'CREATE INDEX auth_session_expiry ON auth_sessions(expires_at)',
    'auth_rate_limits': '''CREATE TABLE auth_rate_limits (
        id TEXT NOT NULL PRIMARY KEY,
        event TEXT NOT NULL,
        subject_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )''',
    'auth_rate_window': 'CREATE INDEX auth_rate_window ON auth_rate_limits(event,subject_hash,created_at)',
}

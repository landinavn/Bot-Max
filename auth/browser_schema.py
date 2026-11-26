"""Additive immutable migration v3: single-use browser login contexts."""
NAME = 'browser_bound_email_login'
DDL = {
    'auth_browser_contexts': '''CREATE TABLE auth_browser_contexts (
        context_mac TEXT NOT NULL PRIMARY KEY,
        expires_at INTEGER NOT NULL,
        consumed_at INTEGER
    )''',
    'auth_browser_expiry': 'CREATE INDEX auth_browser_expiry ON auth_browser_contexts(expires_at)',
    'auth_browser_challenges': '''CREATE TABLE auth_browser_challenges (
        challenge_id TEXT NOT NULL PRIMARY KEY REFERENCES auth_challenges(challenge_id) ON DELETE CASCADE,
        context_mac TEXT NOT NULL REFERENCES auth_browser_contexts(context_mac) ON DELETE CASCADE
    )''',
    'auth_browser_owner': 'CREATE INDEX auth_browser_owner ON auth_browser_challenges(context_mac)',
}

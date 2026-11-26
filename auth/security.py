import hashlib
import hmac
import re
import secrets
from uuid import UUID


def digest(secret, purpose, *values):
    # Length-prefixing prevents ambiguous concatenations; domain separates uses of the key.
    payload = ''.join(str(len(v.encode()))+':'+v for v in (purpose,*values)).encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def generate_code(digits):
    return str(secrets.randbelow(10**digits)).zfill(digits)


def generate_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def challenge_id(value):
    if not isinstance(value,str) or len(value)!=36:
        return None
    try:
        return value if str(UUID(value))==value else None
    except ValueError:
        return None

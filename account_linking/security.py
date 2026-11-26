import secrets
import re
from auth.security import digest

# Exactly 32 symbols, excluding 0/1/I/O: 16 random symbols = 80 bits.
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def new_code():
    raw = ''.join(secrets.choice(ALPHABET) for _ in range(16))
    return '-'.join(raw[i:i+4] for i in range(0,16,4))


def code_mac(secret, value):
    if not isinstance(value,str) or len(value)>64:
        value = ''
    value = value.strip().upper().replace('-','')
    if not re.fullmatch('['+ALPHABET+']{16}',value):
        value = 'invalid'
    return digest(secret,'max-link-code-v1',value)


def subject_mac(secret, purpose, value):
    return digest(secret,'max-link-'+purpose+'-v1',str(value))

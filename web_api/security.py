import hmac
import ipaddress
import re
import secrets

from auth.security import digest


class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def origin_check(request, settings, *, required=False):
    values=request.headers.getlist('origin')
    if len(values)>1 or (values and values[0] not in settings.origins) or (required and not values):
        raise APIError(403,'origin_denied')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise APIError(403,'origin_denied')


def client_source(request, settings):
    try:
        peer=ipaddress.ip_address(request.client.host)
        if isinstance(peer,ipaddress.IPv6Address) and peer.ipv4_mapped:
            peer=peer.ipv4_mapped
        if any(peer in ipaddress.ip_network(p) for p in settings.trusted_proxies):
            values=request.headers.getlist('x-real-ip')
            if len(values)!=1:
                raise ValueError()
            peer=ipaddress.ip_address(values[0])
            if isinstance(peer,ipaddress.IPv6Address) and peer.ipv4_mapped:
                peer=peer.ipv4_mapped
        return peer.compressed
    except (ValueError,AttributeError):
        raise APIError(403,'source_denied') from None


def new_preauth(settings, now):
    value=secrets.token_urlsafe(32)+'.'+str(int(now))
    return value+'.'+digest(settings.secret,'preauth',value)


def browser_context(settings, value, now):
    if not isinstance(value,str) or len(value)>130:
        raise APIError(403,'preauth_required')
    parts=value.split('.')
    if len(parts)!=3 or not re.fullmatch(r'[A-Za-z0-9_-]{43}',parts[0]) or not re.fullmatch(r'[0-9]{1,12}',parts[1]):
        raise APIError(403,'preauth_required')
    issued=int(parts[1]); expected=digest(settings.secret,'preauth','.'.join(parts[:2]))
    if not re.fullmatch(r'[a-f0-9]{64}',parts[2]) or not hmac.compare_digest(expected,parts[2]) or not issued<=now<issued+900:
        raise APIError(403,'preauth_required')
    return digest(settings.secret,'browser',value),issued+900


def csrf(settings, context):
    return digest(settings.secret,'csrf',context)


def csrf_check(request, settings, context):
    values=request.headers.getlist('x-csrf-token')
    if len(values)!=1 or not re.fullmatch(r'[a-f0-9]{64}',values[0]) or not hmac.compare_digest(values[0],csrf(settings,context)):
        raise APIError(403,'csrf_denied')


def set_cookie(response, settings, name, value, age):
    response.set_cookie(name,value,max_age=max(0,int(age)),httponly=True,secure=settings.secure_cookies,
                        samesite='lax',path='/')


def clear_cookie(response, settings, name):
    response.delete_cookie(name,path='/',httponly=True,secure=settings.secure_cookies,samesite='lax')

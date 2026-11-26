"""Explicit application factory: no global app, config reads, migrations or network IO."""
import time
import logging
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from starlette.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from urllib.parse import urlsplit

from accounts.errors import AccountError, AccountStorageError, AccountStoreBusy, SchemaCompatibilityError
from auth.errors import InvalidCode, InvalidSession, RateLimited
from auth.service import AuthService
from auth.settings import AuthSettings
from .config import APISettings
from .schemas import ErrorResponse
from .security import APIError


def error_response(status, code):
    return JSONResponse({'error':{'code':code}},status_code=status)


class Boundary:
    """Bound body bytes before FastAPI JSON parsing, including chunked bodies."""
    def __init__(self,app,settings):
        self.app,self.settings=app,settings

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        s=self.settings
        started=False;finished=False
        async def safe_send(message):
            nonlocal started,finished
            if message['type']=='http.response.start':
                started=True
                headers=[(k,v) for k,v in message.get('headers',[]) if k.lower()!=b'cache-control']
                headers.extend([(b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),
                                (b'x-robots-tag',b'noindex, nofollow')])
                message={**message,'headers':headers}
            if message['type']=='http.response.body' and not message.get('more_body',False):finished=True
            await send(message)
        async def reject(status,code):
            await error_response(status,code)(scope,receive,safe_send)
        raw=scope.get('headers',[])
        def values(key):return [v.decode('latin1') for k,v in raw if k.lower()==key]
        hosts=values(b'host')
        if len(hosts)!=1 or hosts[0] not in {urlsplit(o).netloc for o in s.origins}:
            return await reject(400,'host_denied')
        origins=values(b'origin')
        if len(origins)>1 or (origins and origins[0] not in s.origins) or (scope['method'] not in ('GET','HEAD','OPTIONS') and not origins):
            return await reject(403,'origin_denied')
        if 'cross-site' in values(b'sec-fetch-site'):
            return await reject(403,'origin_denied')
        # Reject duplicate protected cookies rather than guessing which one authenticates.
        cookie_parts=';'.join(values(b'cookie')).split(';')
        for name in (s.session_cookie,s.preauth_cookie):
            if sum(p.strip().split('=',1)[0]==name for p in cookie_parts)>1:
                return await reject(400,'invalid_request')
        lengths=values(b'content-length')
        if len(lengths)>1 or (lengths and (not lengths[0].isdigit() or len(lengths[0])>10)):
            return await reject(400,'invalid_request')
        if lengths and int(lengths[0])>s.body_limit:
            return await reject(413,'body_too_large')
        chunks=[];size=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect':return
            chunk=message.get('body',b'');size+=len(chunk)
            if size>s.body_limit:return await reject(413,'body_too_large')
            chunks.append(chunk)
            if not message.get('more_body'):break
        if scope['method']=='POST':
            content_types=values(b'content-type')
            if len(content_types)!=1 or content_types[0].split(';')[0].strip().lower()!='application/json':
                return await reject(415,'json_required')
        delivered=False
        async def buffered():
            nonlocal delivered
            if not delivered:
                delivered=True
                return {'type':'http.request','body':b''.join(chunks),'more_body':False}
            return await receive()
        try:
            await self.app(scope,buffered,safe_send)
        except Exception:
            # Stop Starlette's outer error middleware from re-raising sensitive exceptions
            # into Uvicorn's traceback logger. Only a constant diagnostic is emitted.
            logging.getLogger(__name__).warning('Web API internal failure')
            if not started:
                await error_response(503,'service_unavailable')(scope,receive,safe_send)
            elif not finished:
                await safe_send({'type':'http.response.body','body':b'','more_body':False})


def create_app(settings: APISettings | None=None, *, sender=None, auth_settings=None, clock=time.time):
    settings=settings or APISettings.from_env()
    if sender is None:
        from auth.smtp import SMTPEmailSender, SMTPSettings
        sender=SMTPEmailSender(SMTPSettings.from_env())
    auth_settings=auth_settings or AuthSettings(secret=settings.secret,environment=settings.environment,
                                              service_name='Открытки и Карта желаний | Нейрофото')
    if auth_settings.secret!=settings.secret or auth_settings.environment!=settings.environment:
        raise ValueError('Inconsistent auth settings')
    if auth_settings.code_digits!=6 or auth_settings.code_ttl>900:
        raise ValueError('HTTP contract requires six digit codes and a code TTL at most 900 seconds')
    app=FastAPI(title='Wish browser auth API',version='1.2A.1',debug=False,
                docs_url=None if settings.environment=='production' else '/docs',redoc_url=None,
                openapi_url=None if settings.environment=='production' else '/openapi.json',
                responses={status:{'model':ErrorResponse} for status in (400,401,403,409,413,415,422,429,503)})
    app.state.settings=settings
    app.state.auth=AuthService(settings.database,auth_settings,sender,clock=clock,timeout=settings.database_timeout)
    if settings.development_origins:
        app.add_middleware(CORSMiddleware,allow_origins=list(settings.development_origins),
            allow_credentials=True,allow_methods=['GET','POST'],allow_headers=['Content-Type','X-CSRF-Token'],max_age=600)
    app.add_middleware(Boundary,settings=settings)

    @app.exception_handler(APIError)
    async def api_error(request,error):return error_response(error.status,error.code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,error):return error_response(422,'invalid_request')

    @app.exception_handler(HTTPException)
    async def http_error(request,error):return error_response(error.status_code,'not_found' if error.status_code==404 else 'request_denied')

    for exception,status,code in ((InvalidCode,400,'invalid_code'),(InvalidSession,401,'unauthenticated'),
        (RateLimited,429,'rate_limited'),(AccountStoreBusy,503,'store_unavailable'),
        (AccountStorageError,503,'store_unavailable'),(SchemaCompatibilityError,503,'not_ready'),
        (AccountError,422,'invalid_request')):
        async def handler(request,error,_status=status,_code=code):return error_response(_status,_code)
        app.add_exception_handler(exception,handler)

    @app.exception_handler(Exception)
    async def unexpected(request,error):
        # Never serialize/log raw exception text, request bodies or credentials.
        return JSONResponse({'error':{'code':'service_unavailable'}},status_code=503,headers={'Cache-Control':'no-store'})

    from .routes import auth, account, health
    for router in (health.router,auth.router,account.router):app.include_router(router)
    def contract():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema=get_openapi(title=app.title,version=app.version,routes=app.routes)
        schema['components']['securitySchemes']={
            'sessionCookie':{'type':'apiKey','in':'cookie','name':settings.session_cookie},
            'preauthCookie':{'type':'apiKey','in':'cookie','name':settings.preauth_cookie},
            'csrfHeader':{'type':'apiKey','in':'header','name':'X-CSRF-Token'},
        }
        for path, operations in schema['paths'].items():
            for method, operation in operations.items():
                if method=='post':
                    operation.setdefault('parameters',[]).append({'name':'Origin','in':'header','required':True,
                        'schema':{'type':'string'},'description':'Exact configured origin; absent Origin is rejected.'})
                    session=path.endswith('/logout') or path.endswith('/logout-all')
                    operation['security']=[{'sessionCookie' if session else 'preauthCookie':[], 'csrfHeader':[]}]
                elif path=='/api/v1/me':
                    operation['security']=[{'sessionCookie':[]}]
        app.openapi_schema=schema
        return schema
    app.openapi=contract
    return app

from dataclasses import asdict
from fastapi import APIRouter, Request, Response
from ..dependencies import current_account, mutation, optional_account, service
from ..schemas import CSRFResponse, EmailRequest, CodeVerify, EmptyRequest, RequestResponse, VerifyResponse, OKResponse
from ..security import APIError, browser_context, clear_cookie, csrf, new_preauth, origin_check, set_cookie, client_source

router=APIRouter(prefix='/api/v1/auth',tags=['auth'])


@router.get('/csrf',response_model=CSRFResponse)
async def csrf_token(request: Request, response: Response):
    s=request.app.state.settings
    origin_check(request,s)
    account=await optional_account(request)
    value=request.cookies.get(s.session_cookie) if account else request.cookies.get(s.preauth_cookie)
    if not account:
        try:
            auth=service(request)
            context=browser_context(s,value,auth.clock())
            if context[1]-int(auth.clock()) < auth.settings.code_ttl or not await auth.repository.browser_context_available(context):
                raise APIError(403,'preauth_required')
        except APIError:
            value=new_preauth(s,service(request).clock())
            set_cookie(response,s,s.preauth_cookie,value,900)
    return {'csrf_token':csrf(s,value)}


async def preauth(request):
    s=request.app.state.settings
    value=request.cookies.get(s.preauth_cookie)
    origin_check(request,s,required=True)
    if await optional_account(request):
        raise APIError(409,'already_authenticated')
    mutation(request,value or '')
    return browser_context(s,value,service(request).clock())


@router.post('/email/request',response_model=RequestResponse)
async def request_code(body: EmailRequest,request: Request):
    context=await preauth(request)
    if context[1]-int(service(request).clock()) < service(request).settings.code_ttl:
        raise APIError(403,'preauth_required')
    result=await service(request).request_email_code(body.email,source=client_source(request,request.app.state.settings),browser_context=context)
    return {'challenge_id':result.challenge_id,'expires_in':result.expires_in}


@router.post('/email/verify',response_model=VerifyResponse)
async def verify_code(body: CodeVerify,request: Request,response: Response):
    context=await preauth(request)
    auth=service(request); s=request.app.state.settings
    result=await auth.verify_email_code(body.challenge_id,body.code,source=client_source(request,s),browser_context=context)
    set_cookie(response,s,s.session_cookie,result.token,result.expires_at-int(auth.clock()))
    clear_cookie(response,s,s.preauth_cookie)
    account=asdict(result.account); account.pop('updated_at')
    return {'account':account,'csrf_token':csrf(s,result.token)}


@router.post('/logout',response_model=OKResponse)
async def logout(body: EmptyRequest,request: Request,response: Response):
    s=request.app.state.settings; token=request.cookies.get(s.session_cookie)
    mutation(request,token or '')
    await current_account(request)
    await service(request).logout(token)
    clear_cookie(response,s,s.session_cookie);clear_cookie(response,s,s.preauth_cookie)
    return {'status':'ok'}


@router.post('/logout-all',response_model=OKResponse)
async def logout_all(body: EmptyRequest,request: Request,response: Response):
    s=request.app.state.settings; token=request.cookies.get(s.session_cookie)
    mutation(request,token or '')
    await current_account(request)
    await service(request).logout_all(token)
    clear_cookie(response,s,s.session_cookie);clear_cookie(response,s,s.preauth_cookie)
    return {'status':'ok'}

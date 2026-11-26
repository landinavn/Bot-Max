from fastapi import Request
from auth.errors import InvalidSession
from .security import APIError, csrf_check, origin_check


def service(request: Request):
    return request.app.state.auth


async def optional_account(request: Request):
    token=request.cookies.get(request.app.state.settings.session_cookie)
    if not token:
        return None
    try:
        return await service(request).get_current_account(token)
    except InvalidSession:
        return None


async def current_account(request: Request):
    settings=request.app.state.settings
    origin_check(request,settings)
    if request.query_params:
        raise APIError(422,'invalid_request')
    token=request.cookies.get(settings.session_cookie)
    return await service(request).get_current_account(token)


def mutation(request: Request, context):
    settings=request.app.state.settings
    origin_check(request,settings,required=True)
    csrf_check(request,settings,context)
    if request.query_params:
        raise APIError(422,'invalid_request')

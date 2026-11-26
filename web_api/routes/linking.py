from dataclasses import asdict
from fastapi import APIRouter, Request
from ..dependencies import current_account, mutation, service as auth_service
from ..security import APIError, client_source
from ..schemas import StrictSchema, EmptyRequest
from typing import Annotated, Literal
from pydantic import Field

router=APIRouter(prefix='/api/v1/account',tags=['account linking'])


class LinkOperation(StrictSchema):
    operation_id: str
    status: Literal['pending','max_confirmed','completed','cancelled','expired','conflict']
    expires_at: int
    max_confirmed: bool
    max_hint: str | None


class LinkStatus(StrictSchema):
    operation: LinkOperation | None


class LinkStart(LinkStatus):
    code: str
    bot_url: str
    command: str


class LinkID(StrictSchema):
    operation_id: Annotated[str,Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')]


class Identity(StrictSchema):
    provider: str
    verified: bool


class Identities(StrictSchema):
    identities: list[Identity]


def linking(request):
    service=request.app.state.linking
    if service is None:
        raise APIError(503,'linking_disabled')
    return service


async def token(request,*,change=False):
    value=request.cookies.get(request.app.state.settings.session_cookie)
    if change:
        mutation(request,value or '')
    await current_account(request)
    return value


def result(operation):
    return {'operation':asdict(operation) if operation else None}


@router.get('/identities',response_model=Identities)
async def identities(request: Request):
    account=await current_account(request)
    items=await auth_service(request).repository.accounts.list_account_identities(account.id)
    return {'identities':[{'provider':provider,'verified':True} for provider in sorted({item.provider for item in items})]}


@router.post('/max-link/start',response_model=LinkStart)
async def start(body: EmptyRequest,request: Request):
    value=await token(request,change=True)
    started=await linking(request).start(value,source=client_source(request,request.app.state.settings))
    return {**result(started.operation),'code':started.code,
            'bot_url':'https://max.ru/se14596751_1_bot','command':'/link_account'}


@router.get('/max-link/status',response_model=LinkStatus)
async def status(request: Request):
    value=await token(request)
    return result(await linking(request).status(value))


@router.post('/max-link/confirm',response_model=LinkStatus)
async def confirm(body: LinkID,request: Request):
    value=await token(request,change=True)
    return result(await linking(request).confirm(value,body.operation_id,source=client_source(request,request.app.state.settings)))


@router.post('/max-link/cancel',response_model=LinkStatus)
async def cancel(body: LinkID,request: Request):
    value=await token(request,change=True)
    return result(await linking(request).cancel(value,body.operation_id,source=client_source(request,request.app.state.settings)))

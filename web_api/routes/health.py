from fastapi import APIRouter, Request
from accounts.migrations import inspect_schema
from ..schemas import OKResponse
from ..security import APIError

router=APIRouter(prefix='/api/v1/health',tags=['health'])


@router.get('/live',response_model=OKResponse)
async def live():
    return {'status':'ok'}


@router.get('/ready',response_model=OKResponse)
async def ready(request: Request):
    s=request.app.state.settings
    if await inspect_schema(s.database,timeout=s.database_timeout)!='applied':
        raise APIError(503,'not_ready')
    # Local schema/adapter configuration readiness, not a promise of SMTP delivery.
    return {'status':'ok'}

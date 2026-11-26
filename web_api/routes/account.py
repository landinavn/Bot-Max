from fastapi import APIRouter, Request
from ..dependencies import current_account
from ..schemas import AccountResponse

router=APIRouter()


@router.get('/api/v1/me',response_model=AccountResponse,tags=['account'])
async def me(request: Request):
    account=await current_account(request)
    return {'id':account.id,'created_at':account.created_at}

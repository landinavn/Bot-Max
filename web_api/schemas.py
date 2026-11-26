from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from accounts.models import normalize_email


class StrictSchema(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class EmailRequest(StrictSchema):
    email: Annotated[str,Field(min_length=3,max_length=254)]

    @field_validator('email')
    @classmethod
    def email_format(cls,value):
        return normalize_email(value)


class CodeVerify(StrictSchema):
    challenge_id: Annotated[str,Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')]
    code: Annotated[str,Field(pattern=r'^[0-9]{6}$')]


class EmptyRequest(StrictSchema):
    pass


class AccountResponse(StrictSchema):
    id: str
    created_at: str


class CSRFResponse(StrictSchema):
    csrf_token: str


class VerifyResponse(StrictSchema):
    account: AccountResponse
    csrf_token: str


class RequestResponse(StrictSchema):
    challenge_id: str
    expires_in: int
    status: Literal['requested']='requested'
    message: str='Запрос принят. Если письмо придёт, используйте код из него. Доставка не гарантируется.'


class OKResponse(StrictSchema):
    status: Literal['ok']='ok'


class ErrorDetail(StrictSchema):
    code: str


class ErrorResponse(StrictSchema):
    error: ErrorDetail

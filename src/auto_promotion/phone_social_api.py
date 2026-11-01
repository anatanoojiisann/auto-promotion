"""Private preparations and separately authenticated one-use runtime permission."""

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from .phone_reddit_api import AuthorizationInput


class PreparationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    target_author: str = Field(min_length=1, max_length=128)
    target_text: str = Field(min_length=1, max_length=2000)
    discard_after_prepare: bool = True


class RegistrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MembershipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_url: str = Field(max_length=2048)
    target_title: str = Field(min_length=1,max_length=512)


def mount(app, service):
    @app.post("/api/phone-social/accounts/{account_id}/membership")
    def membership(account_id:str,body:MembershipInput):
        return service.membership(account_id,body.target_url,body.target_title)
    @app.post("/api/phone-social/accounts/{account_id}/register")
    def register(account_id: str, body: RegistrationInput | None = None):
        return service.register(account_id)

    @app.post("/api/phone-social/items/{item_id}/prepare")
    def prepare(item_id: str, body: PreparationInput):
        return service.prepare(item_id, body.model_dump(exclude={"discard_after_prepare"}),
                               discard=body.discard_after_prepare)

    @app.post("/internal/phone-social/v1/authorize")
    def authorize(body: AuthorizationInput, request: Request):
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        return service.authorize(body.model_dump(), bearer)

    @app.post("/internal/phone-social/v1/telegram-receipt")
    def telegram_receipt(body:AuthorizationInput,request:Request):
        return service.telegram_receipt(body.model_dump(),request.headers.get("authorization","").removeprefix("Bearer "))

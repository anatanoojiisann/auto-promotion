"""Private preparation routes and a separately authenticated runtime submit guard."""

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field


class PrepareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    post_id: str = Field(pattern=r"^[a-z0-9]{3,20}$")
    subreddit: str = Field(pattern=r"^[A-Za-z0-9_]{2,40}$")
    title: str = Field(min_length=1, max_length=512)
    author: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    discard_after_prepare: bool = False


class RegistrationInput(PrepareInput):
    target_url: str = Field(min_length=1, max_length=2048)
    text: str = Field(min_length=1, max_length=2000)


class AuthorizationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    native_command_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    execution_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    device_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


def mount(app, service):
    @app.post("/api/phone-reddit/accounts/{account_id}/register")
    def register(account_id: str, body: RegistrationInput):
        return service.register(account_id, body.model_dump())

    @app.post("/api/phone-reddit/items/{item_id}/prepare")
    def prepare(item_id: str, body: PrepareInput):
        metadata = body.model_dump(exclude={"discard_after_prepare"})
        return service.prepare(item_id, metadata, discard=body.discard_after_prepare)

    @app.post("/internal/phone-reddit/v1/authorize")
    def authorize(body: AuthorizationInput, request: Request):
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        return service.authorize(body.model_dump(), bearer)

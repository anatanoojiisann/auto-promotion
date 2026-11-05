"""Round routes share the existing authenticated local API boundary."""

from pydantic import BaseModel, ConfigDict, Field


class RoundInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[str] = Field(min_length=1, max_length=6)
    request_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")


def mount(app, service):
    app.state.phone_rounds = service

    @app.get("/api/phone-round-candidates/{account_id}")
    def get_candidates(account_id: str):
        from .phone_round_readiness import shortlist

        return shortlist(service.engine, account_id)

    @app.get("/api/phone-rounds/{round_id}/preflight")
    def preflight_round(round_id: str):
        from .phone_round_readiness import preflight

        return preflight(service, round_id)

    @app.post("/api/phone-rounds")
    def create_round(body: RoundInput):
        return service.create(body.task_ids, body.request_key)

    @app.get("/api/phone-rounds/{round_id}")
    def get_round(round_id: str):
        return service.get(round_id)

    @app.post("/api/phone-rounds/{round_id}/start")
    def start_round(round_id: str):
        return service.start(round_id)

    @app.get("/api/phone-rounds/{round_id}/platforms/{platform}/preflight")
    def preflight_platform(round_id: str, platform: str):
        from .phone_round_readiness import preflight

        return preflight(service, round_id, platforms={platform})

    @app.post("/api/phone-rounds/{round_id}/platforms/{platform}/start")
    def start_platform(round_id: str, platform: str):
        return service.start(round_id, platform)

    @app.post("/api/phone-rounds/{round_id}/platforms/{platform}/stop")
    def stop_platform(round_id: str, platform: str):
        return service.stop_platform(round_id, platform)

    @app.post("/api/phone-rounds/{round_id}/stop")
    def stop_round(round_id: str):
        return service.stop(round_id)

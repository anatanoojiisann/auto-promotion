"""Protocol upgrade, missing-hardware status, and read-only legacy reconciliation."""

import json

import httpx
import pytest
from phone_fleet.protocol import VERSION

from auto_promotion.backends.common import BackendError
from auto_promotion.phone_lab import apps, initialize
from auto_promotion.phone_protocol import capability_status, legacy_result, require_capabilities
from auto_promotion.phone_software_check import check


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"version": VERSION, "operations": {"observe": []}},
        {"version": "private token", "operations": {"observe": "supported"}},
        {"version": VERSION, "operations": []},
    ],
)
def test_malformed_capabilities_fail_closed_without_echoing_body(value):
    result = capability_status(value)
    assert not result["software_ready"] and not result["device_ready"]
    assert "private token" not in json.dumps(result)


def test_missing_phone_is_waiting_and_protocol_mismatch_requires_upgrade():
    result = capability_status({"version": VERSION, "operations": {"observe": "unsupported"}})
    assert result["software_ready"] and result["state"] == "waiting_for_device"
    assert result["device_ready"] is False and result["submission_authorized"] is False
    old = capability_status(
        {"version": "fleet.internal/0.1", "operations": {"observe": "supported"}}
    )
    assert old["state"] == "upgrade_required" and old["software_ready"] is False
    with pytest.raises(BackendError, match="ProtocolMismatch"):
        require_capabilities({"version": "fleet.internal/0.1"}, "observe")
    with pytest.raises(BackendError, match="DeviceNotReady"):
        require_capabilities(
            {"version": VERSION, "operations": {"observe": "unsupported"}}, "observe"
        )


def test_legacy_success_cannot_become_publication_confirmation():
    request = {"version": "fleet.internal/0.1", "business_task_id": "original-task"}
    result = legacy_result(
        {"id": "original-execution", "request": request, "status": "succeeded"},
        request,
        "original-execution",
    )
    assert result["legacy_read_only"] and result["status"] == "unknown" and not result["verified"]
    assert result["reported_state"] == "succeeded"
    with pytest.raises(BackendError, match="CorrelationMismatch"):
        legacy_result(
            {"id": "another-execution", "request": request}, request, "original-execution"
        )


@pytest.mark.asyncio
async def test_six_app_integration_uses_real_fleet_api_without_network_or_publication(
    tmp_path, monkeypatch
):
    import socket

    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("external network forbidden")),
    )
    result = await check(tmp_path)
    assert result["protocol"] == VERSION and result["mode"] == "simulation"
    assert len(result["platforms"]) == 6 and result["outbound_attempts"] == 0
    assert (
        result["real_publications_confirmed"] == 0
        and result["hardware_validation"] == "not_started"
    )
    operations = [op for row in result["platforms"] for op in row["operations"]]
    assert len({op["execution_id"] for op in operations}) == 12
    assert all(op["simulated"] and not op["publication_verified"] for op in operations)
    assert all(
        row["manifest_unchanged"] and row["item_status"] == "not_attempted"
        for row in result["platforms"]
    )


@pytest.mark.asyncio
async def test_isolated_lab_is_new_version_waiting_for_hardware_even_with_live_env(
    tmp_path, monkeypatch
):
    from phone_fleet.client import Client

    monkeypatch.setenv("AUTO_PROMOTION_LIVE_WRITES", "1")
    ap, fleet = apps(tmp_path, 18452, 18450)
    service = ap.state.phone_preparations
    config = json.loads((tmp_path / "lab.private.json").read_text())
    await service.client.close()
    service.client = Client(
        "http://127.0.0.1:18450",
        config["business_token"],
        development=True,
        transport=httpx.ASGITransport(fleet),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(ap), base_url="http://testserver"
        ) as client:
            assert (await client.get("/api/phone-readiness")).status_code == 401
            health = (await client.get("/health")).json()
            assert not health["live_writes"] and not health["worker"]
            response = await client.get(
                "/api/phone-readiness",
                headers={"Authorization": "Bearer " + config["operator_token"]},
            )
            data = response.json()
            assert (
                data["state"] == "software_ready"
                and data["services"][0]["state"] == "waiting_for_device"
            )
            assert data["hardware_validation"] == "not_started" and not data["publication_verified"]
            assert config["business_token"] not in json.dumps(data)
    finally:
        await service.client.close()


def test_lab_preserves_config_and_refuses_other_directory_or_ports(tmp_path):
    config = initialize(tmp_path, 18452, 18450)
    assert initialize(tmp_path, 18452, 18450) == config
    with pytest.raises(ValueError, match="ConfigurationMismatch"):
        initialize(tmp_path, 18453, 18450)
    other = tmp_path / "unrelated"
    other.mkdir()
    (other / "existing.db").touch()
    with pytest.raises(ValueError, match="EmptyDirectory"):
        initialize(other, 18452, 18450)


@pytest.mark.asyncio
async def test_software_check_is_bounded_and_does_not_report_timed_out_work_as_passed(
    tmp_path, monkeypatch
):
    import asyncio

    import auto_promotion.phone_software_check as module

    cancelled = []

    async def stalled(root):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(module, "_check", stalled)
    with pytest.raises(TimeoutError):
        await module.check(tmp_path, timeout=0.02)
    assert cancelled == [True]

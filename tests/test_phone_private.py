import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from auto_promotion.core import Conflict
from auto_promotion.phone_private import write_worker_config


class Config:
    service_token = SecretStr("fake-service-token")
    runtime_token = SecretStr("fake-runtime-token")
    oauth_client_secret = SecretStr("fake-oauth-secret")
    reddit_read_access_token = None

    def model_dump(self, **kwargs):
        return {"mode": "development", "service_token": "masked", "runtime_token": "masked"}


def test_missing_or_unsafe_directory_never_writes_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("AP_PHONE_PRIVATE_DIR", raising=False)
    with pytest.raises(Conflict, match="PhonePrivateWorkerDirectoryRequired"):
        write_worker_config(tmp_path / "db.sqlite", Config(), "social")
    monkeypatch.setenv("AP_PHONE_PRIVATE_DIR", str(tmp_path))
    monkeypatch.setattr("auto_promotion.phone_private.private_directory_is_safe", lambda _: False)
    with pytest.raises(Conflict, match="PhonePrivateWorkerDirectoryRequired"):
        write_worker_config(tmp_path / "db.sqlite", Config(), "social")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["social", "reddit"])
def test_worker_handoff_stays_in_checked_directory_and_preserves_database(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setenv("AP_PHONE_PRIVATE_DIR", str(tmp_path))
    monkeypatch.setattr("auto_promotion.phone_private.private_directory_is_safe", lambda _: True)
    path = Path(write_worker_config(tmp_path / "db.sqlite", Config(), kind))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.parent == tmp_path
    assert data["database"] == str((tmp_path / "db.sqlite").resolve())
    assert data["config"]["runtime_token"] == "fake-runtime-token"
    assert not list(tmp_path.glob("*.tmp"))


def test_relative_directory_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("AP_PHONE_PRIVATE_DIR", "relative-private")
    with pytest.raises(Conflict, match="PhonePrivateWorkerDirectoryRequired"):
        write_worker_config(tmp_path / "db.sqlite", Config(), "social")

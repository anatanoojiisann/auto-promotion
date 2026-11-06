"""Phone work must preserve a matching pre-existing desktop or browser connection."""

import pytest

from auto_promotion.backends.common import read_profile, save_profile
from auto_promotion.core import Conflict
from auto_promotion.models import digest
from auto_promotion.routing import route_for, task_route

pytest_plugins = ("test_phone_social",)


@pytest.mark.parametrize("pilot", ["instagram", "x", "tiktok", "facebook"], indirect=True)
def test_explicit_phone_route_preserves_matching_existing_connection(pilot):
    s = pilot
    prior = read_profile(s.app, s.account["backend_label"])
    connection = {
        "instagram": "meta_desktop",
        "facebook": "meta_desktop",
        "tiktok": "tiktok_desktop",
        "x": None,
    }[s.app]
    existing = {"identity": prior["identity"], "browser_channel": "msedge", "preserved": "marker"}
    if connection is not None:
        existing["connection_type"] = connection
    save_profile(s.app, s.account["backend_label"], existing)
    before = read_profile(s.app, s.account["backend_label"])
    account = s.engine.account(s.account["id"])
    ordinary = route_for(account)
    assert ordinary.transport != "phone"
    route = task_route(account, {}, "phone", s.engine.adapters)
    assert route.pool == s.app + ".phone" and route.resource == "phone:" + digest("device-test")
    manual = task_route(account, {"execution_mode": "manual"}, "phone", s.engine.adapters)
    assert manual.transport == "manual" and manual.resource is None
    assert task_route(account, {}, "inherit", s.engine.adapters) == ordinary
    result = s.service.register(s.account["id"])
    assert result["identity_verified"] is True
    assert read_profile(s.app, s.account["backend_label"]) == before
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    assert all(
        r.method != "POST" or b'"operation":"social_submit"' not in r.content
        for r in s.fake.requests
    )


@pytest.mark.parametrize("pilot", ["instagram", "x", "tiktok", "facebook"], indirect=True)
def test_mismatched_existing_identity_blocks_phone_without_dispatch(pilot):
    s = pilot
    prior = read_profile(s.app, s.account["backend_label"])
    connection = {
        "instagram": "meta_desktop",
        "facebook": "meta_desktop",
        "tiktok": "tiktok_desktop",
        "x": None,
    }[s.app]
    existing = {"identity": prior["identity"], "browser_channel": "msedge", "preserved": "marker"}
    if connection is not None:
        existing["connection_type"] = connection
    save_profile(s.app, s.account["backend_label"], existing)
    bindings = dict(s.service.config.bindings)
    mismatched = "facebook:99999" if s.app == "facebook" else "@different_identity"
    bindings[s.account["id"]] = bindings[s.account["id"]].model_copy(
        update={"identity": mismatched}
    )
    s.service.config = s.service.config.model_copy(update={"bindings": bindings})
    before = read_profile(s.app, s.account["backend_label"])
    route = task_route(s.engine.account(s.account["id"]), {}, "phone", s.engine.adapters)
    assert route.pool == s.app + ".phone" and route.resource is None
    count = len(s.fake.requests)
    with pytest.raises(Conflict, match="PhoneSocialExistingConnectionPreserved"):
        s.service.register(s.account["id"])
    assert len(s.fake.requests) == count
    assert read_profile(s.app, s.account["backend_label"]) == before


@pytest.mark.parametrize("pilot", ["instagram", "x"], indirect=True)
def test_stale_account_identity_hash_blocks_shared_phone_without_dispatch(pilot):
    s = pilot
    prior = read_profile(s.app, s.account["backend_label"])
    existing = {"identity": prior["identity"], "browser_channel": "msedge"}
    if s.app == "instagram":
        existing["connection_type"] = "meta_desktop"
    save_profile(s.app, s.account["backend_label"], existing)
    with s.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET identity_hash=? WHERE id=?", ("f" * 64, s.account["id"]))
    route = task_route(s.engine.account(s.account["id"]), {}, "phone", s.engine.adapters)
    assert route.resource is None
    request_count = len(s.fake.requests)
    with pytest.raises(Conflict, match="PhoneSocialExistingConnectionPreserved"):
        s.service.register(s.account["id"])
    assert len(s.fake.requests) == request_count

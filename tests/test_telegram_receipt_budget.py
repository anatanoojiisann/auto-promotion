"""Prepaid receipt reads remain bounded by the shared real-account budget."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from auto_promotion.backends.common import BackendError
from auto_promotion.backends.telegram_reads import ReadBudgetExceeded, TelegramReads
from auto_promotion.phone_telegram import JOIN_SCHEMA, PhoneTelegram
from auto_promotion.store import Store
from auto_promotion.telegram_receipt_budget import reserve_receipt_reads, validate_receipt_reads


def make_guard(tmp_path, monkeypatch, *, identity="42", limit=20):
    monkeypatch.setenv("AP_ACCOUNTS_DIR", str(tmp_path / "accounts"))
    return TelegramReads("test", identity, "r1", {"requests_per_window": limit}, scan_limit=16)


def test_prepaid_receipt_reads_are_charged_to_the_same_account_without_refund(
    tmp_path, monkeypatch
):
    guard = make_guard(tmp_path, monkeypatch)
    reservation = reserve_receipt_reads(guard)
    validate_receipt_reads(make_guard(tmp_path, monkeypatch), reservation)
    competitor = make_guard(tmp_path, monkeypatch)
    competitor.reserve(4)
    with pytest.raises(ReadBudgetExceeded, match="TelegramReadAccountBudgetExhausted"):
        competitor.reserve(1)
    with guard.connect() as db:
        assert (
            db.execute("SELECT used FROM budgets WHERE identity=?", (guard.identity,)).fetchone()[0]
            == 20
        )
    with pytest.raises(BackendError, match="BindingChanged"):
        validate_receipt_reads(make_guard(tmp_path, monkeypatch, identity="43"), reservation)


def test_reservation_refuses_window_change_or_policy_tightening(tmp_path, monkeypatch):
    guard = make_guard(tmp_path, monkeypatch)
    reservation = reserve_receipt_reads(guard)
    with pytest.raises(BackendError, match="Unavailable"):
        validate_receipt_reads(guard, {**reservation, "window_end": reservation["window_end"] + 1})
    with guard.connect() as db:
        db.execute(
            "INSERT INTO read_authority VALUES(?,?,?,?)",
            (
                guard.identity,
                "synthetic",
                1,
                json.dumps({**guard.policy, "requests_per_window": 10}),
            ),
        )
    with pytest.raises(BackendError, match="Unavailable"):
        validate_receipt_reads(guard, reservation)


def test_window_near_expiry_is_rejected_without_refunding_shared_budget(tmp_path, monkeypatch):
    guard = make_guard(tmp_path, monkeypatch)
    guard.reserve(1)
    with guard.connect() as db:
        db.execute("UPDATE budgets SET end=? WHERE identity=?", (time.time() + 10, guard.identity))
        db.execute(
            "UPDATE budgets SET start=? WHERE identity=?", (time.time() - 3590, guard.identity)
        )
    with pytest.raises(BackendError, match="WindowTooShort"):
        reserve_receipt_reads(make_guard(tmp_path, monkeypatch))
    with guard.connect() as db:
        assert (
            db.execute("SELECT used FROM budgets WHERE identity=?", (guard.identity,)).fetchone()[0]
            == 17
        )


def test_competing_receipt_readers_cannot_overspend_reserved_calls(tmp_path, monkeypatch):
    guard = make_guard(tmp_path, monkeypatch)
    reservation = reserve_receipt_reads(guard)
    store = Store(tmp_path / "private.db")
    with store.connect(write=True) as db:
        db.execute("CREATE TABLE phone_social_authorizations(id TEXT PRIMARY KEY,used_at REAL)")
        for statement in JOIN_SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute("INSERT INTO phone_social_authorizations VALUES(?,?)", ("auth", time.time()))
        db.execute(
            "INSERT INTO phone_telegram_receipt_budgets VALUES(?,?,?,?,?,?,0)",
            (
                "auth",
                "account",
                "observer",
                reservation["read_identity"],
                reservation["window_end"],
                reservation["reserved"],
            ),
        )
    phone = SimpleNamespace(store=store)

    def spend(_):
        try:
            PhoneTelegram.consume_receipt_budget(
                phone, "auth", "account", "observer", make_guard(tmp_path, monkeypatch), 1
            )
            return True
        except BackendError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(spend, range(24))) == 16
    assert (
        store.one("SELECT used FROM phone_telegram_receipt_budgets WHERE authorization_id='auth'")[
            "used"
        ]
        == 16
    )
    with pytest.raises(BackendError, match="BindingChanged"):
        PhoneTelegram.consume_receipt_budget(phone, "auth", "other", "observer", guard, 1)

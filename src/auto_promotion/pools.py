"""Versioned operational profiles and reusable account groups; no credentials."""

import uuid

from .core import Conflict, NotFound
from .models import AccountProfile, canonical
from .store import Store, decode


class Pools:
    def __init__(self, engine):
        self.engine, self.store, self.clock = engine, engine.store, engine.clock

    def profile(self, account_id):
        self.engine.account(account_id)
        row = self.store.one("SELECT * FROM account_profiles WHERE account_id=?", (account_id,))
        return (
            decode(row, "payload")
            if row
            else {
                "account_id": account_id,
                "version": 0,
                "payload": AccountProfile().model_dump(),
                "updated_at": None,
            }
        )

    def set_profile(self, account_id, value):
        self.engine.account(account_id)
        # Validate template fields now rather than failing during a large planning run.
        from .planner import render_template

        if value.reply_template:
            render_template(value.reply_template, {})
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO account_profiles VALUES(?,1,?,?) ON CONFLICT(account_id) "
                "DO UPDATE SET version=version+1,payload=excluded.payload,updated_at=excluded.updated_at",
                (account_id, canonical(value.model_dump()), self.clock()),
            )
            Store.audit(db, "profile.updated", {"account_id": account_id}, now=self.clock())
        return self.profile(account_id)

    def get(self, pool_id):
        row = self.store.one("SELECT * FROM account_pools WHERE id=?", (pool_id,))
        if not row:
            raise NotFound("号池不存在")
        result = decode(row, "limits")
        result["account_ids"] = [
            r["account_id"]
            for r in self.store.rows(
                "SELECT account_id FROM pool_members WHERE pool_id=? ORDER BY account_id",
                (pool_id,),
            )
        ]
        result["accounts"] = [self.engine.account(a) for a in result["account_ids"]]
        return result

    def list(self):
        return [
            self.get(r["id"])
            for r in self.store.rows("SELECT id FROM account_pools ORDER BY created_at DESC")
        ]

    def save(self, value, pool_id=None):
        if len(value.account_ids) != len(set(value.account_ids)):
            raise Conflict("号池账号不能重复")
        for account_id in value.account_ids:
            self.engine.account(account_id)
        if pool_id:
            self.get(pool_id)
        pool_id = pool_id or "pool-" + uuid.uuid4().hex[:12]
        now = self.clock()
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO account_pools VALUES(?,?,?,?,1,?,?) ON CONFLICT(id) "
                "DO UPDATE SET name=excluded.name,description=excluded.description,limits=excluded.limits,revision=revision+1,updated_at=excluded.updated_at",
                (
                    pool_id,
                    value.name,
                    value.description,
                    canonical(value.limit.model_dump()),
                    now,
                    now,
                ),
            )
            db.execute("DELETE FROM pool_members WHERE pool_id=?", (pool_id,))
            db.executemany(
                "INSERT INTO pool_members VALUES(?,?)", [(pool_id, a) for a in value.account_ids]
            )
            Store.audit(
                db, "pool.saved", {"pool_id": pool_id, "accounts": len(value.account_ids)}, now=now
            )
        return self.get(pool_id)

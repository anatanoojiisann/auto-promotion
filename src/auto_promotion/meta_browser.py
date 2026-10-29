"""Meta browser work shares the normal queue; the desktop agent owns the UI.

No browser credentials or debugging ports are copied. Listing work, observing an
identity and synchronizing old workflows never starts a task or submits content.
"""

import json
import math
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .backends.common import BackendError, verify_claim
from .models import canonical
from .queue_store import JobQueue, job_scope
from .routing import route_for
from .statistics import Statistics, publication_time
from .store import Store

MAX_OBSERVATION_AGE = 300
LEASE_SECONDS = 180
META = {"facebook", "instagram"}


def event_key(platform, url, payload=None):
    """One conversation across link variants, authors and account assignments."""
    if platform == "tiktok":
        from .tiktok_browser import video_target
        from .tiktok_messages import DM_ACTIONS, dm_event_key, recipient_target

        if (payload or {}).get("action") in DM_ACTIONS:
            return dm_event_key(payload)
        if "/video/" not in urlparse(url).path:
            return "dm-recipient:" + recipient_target(url)
        return "video:" + video_target(url)["video_id"]
    u = urlparse(url)
    hosts = {
        "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com"},
        "instagram": {"instagram.com", "www.instagram.com"},
    }
    if (
        platform not in META
        or u.scheme != "https"
        or u.hostname not in hosts[platform]
        or u.netloc != u.hostname
        or u.fragment
    ):
        raise ValueError("需要明确的 Facebook 或 Instagram HTTPS 原帖／评论链接")
    if platform == "instagram":
        match = re.fullmatch(
            r"/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels)/([A-Za-z0-9_-]+)"
            r"(?:/c/([0-9]+)(?:/r/[0-9]+)?)?/?",
            u.path,
        )
        if not match:
            raise ValueError("Instagram 目标需要原帖或指定评论链接")
        return "media:" + match[1]
    query = parse_qs(u.query)
    group = re.fullmatch(r"/groups/([^/]+)/(?:posts|permalink)/([0-9]+)/?", u.path)
    post = re.fullmatch(r"/([^/]+)/posts/([^/]+)/?", u.path)
    reel = re.fullmatch(r"/reel/([0-9]+)/?", u.path)
    if group:
        return "group-post:" + group[2]
    if post:
        return "post:" + post[2]
    if reel:
        return "reel:" + reel[1]
    if u.path in {"/permalink.php", "/story.php"} and query.get("story_fbid"):
        return "post:" + query["story_fbid"][0]
    raise ValueError("Facebook 目标需要明确的原帖或帖子内评论链接")


def fresh(observation, now):
    at = observation.get("observed_at")
    return type(at) in {int, float} and math.isfinite(at) and 0 <= now - at <= MAX_OBSERVATION_AGE


MODERATION_LABELS = frozenset({"待审核", "Pending approval", "Pending review", "Awaiting approval"})


def verified_pending_moderation(row, account, connection, now):
    """Only a validated, independently observed Facebook review avoids account-wide blocking."""
    if (
        account["platform"] != "facebook"
        or row["status"] != "unknown"
        or row["submission_started"] != 1
    ):
        return False
    try:
        payload = json.loads(row["payload"])
        item_result = json.loads(row["item_result"])
        claim = json.loads(row["claim"])
        claim_result = json.loads(row["claim_result"])
        marker = claim_result["moderation_observation"]
        at = marker.get("observed_at")
        guarded_at = claim_result.get("submission_guarded_at")
        return bool(
            item_result.get("platform_status") == "pending_admin_approval"
            and claim.get("item_id") == row["id"]
            and claim.get("attempt_id") == row["attempt_id"]
            and claim.get("payload") == payload
            and marker.get("item_id") == row["id"]
            and marker.get("attempt_id") == row["attempt_id"]
            and marker.get("payload_hash") == row["payload_hash"]
            and marker.get("account_id") == account["id"]
            and marker.get("identity") == connection["identity"]
            and marker.get("parent_target") == payload.get("target")
            and marker.get("observed_url") == payload.get("target")
            and marker.get("observed_text") == payload.get("text")
            and marker.get("moderation_label") in MODERATION_LABELS
            and all(
                marker.get(k) is True
                for k in (
                    "identity_verified",
                    "author_verified",
                    "parent_verified",
                    "independent_view",
                )
            )
            and isinstance(marker.get("evidence"), str)
            and 0 < len(marker["evidence"]) <= 2000
            and type(guarded_at) in {int, float}
            and math.isfinite(guarded_at)
            and 0 < guarded_at <= now
            and type(at) in {int, float}
            and math.isfinite(at)
            and guarded_at <= at <= now
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def claim_blocker(db, account, payload, item_id, now):
    """A gate inside Engine's write transaction, also covering non-desktop callers."""
    connection = db.execute(
        "SELECT * FROM meta_browser_accounts WHERE account_id=?", (account["id"],)
    ).fetchone()
    if not connection or payload.get("action") not in {
        "comment",
        "reply",
        "send_direct_message",
        "reply_direct_message",
    }:
        return None
    sources = db.execute(
        "SELECT * FROM meta_browser_sources WHERE account_id=?", (account["id"],)
    ).fetchall()
    acknowledged_legacy_items = set()
    for source in sources:
        if (
            source["state"] != "unknown"
            or json.loads(source["details"]).get("native_item_id") == item_id
        ):
            continue
        from .meta_legacy_skip import legacy_skip_item

        skipped = legacy_skip_item(db, source, account, payload, item_id, now)
        if not skipped:
            return "meta_source_unknown"
        acknowledged_legacy_items.add(skipped)
    if any(s["state"] == "busy" for s in sources):
        return "meta_source_busy"
    unsettled = db.execute(
        "SELECT i.id,i.account_id,i.status,i.payload,i.payload_hash,i.attempt_id,i.result item_result, "
        "c.claim,c.result claim_result,c.submission_started FROM items i "
        "LEFT JOIN meta_browser_claims c ON c.item_id=i.id "
        "WHERE i.account_id=? AND i.status IN ('unknown','executing','scheduled') AND i.id<>?",
        (account["id"], item_id),
    ).fetchall()
    if any(
        row["id"] not in acknowledged_legacy_items
        and not verified_pending_moderation(row, account, connection, now)
        for row in unsettled
    ):
        return "meta_outcome_unknown"
    key = event_key(account["platform"], payload["target"], payload)
    previous = db.execute(
        "SELECT item_id FROM meta_browser_contacts WHERE platform=? AND event_key=?",
        (account["platform"], key),
    ).fetchone()
    if previous and previous[0] != item_id:
        return "meta_duplicate_target"
    return None


class MetaBrowser:
    platforms = ("facebook", "instagram")

    def __init__(self, engine=None, *, store=None):
        self.engine = engine
        self.store = engine.store if engine else store
        self.clock = engine.clock if engine else time.time

    @classmethod
    def for_account(cls, database, account):
        if not database or account["platform"] not in cls.platforms:
            return None
        store = Store(database, initialize=False)
        if store.one("SELECT 1 FROM meta_browser_accounts WHERE account_id=?", (account["id"],)):
            return cls(store=store)
        return None

    def connection(self, account_id):
        account = self.store.one("SELECT platform FROM accounts WHERE id=?", (account_id,))
        if not account or account["platform"] not in self.platforms:
            raise ValueError("该账号不属于此桌面浏览器通道")
        row = self.store.one(
            "SELECT * FROM meta_browser_accounts WHERE account_id=?", (account_id,)
        )
        if not row:
            raise ValueError("该账号未接入 Meta 浏览器队列")
        return {
            **row,
            "details": json.loads(row["details"]),
            "observation": json.loads(row["observation"]),
        }

    def release_pending(self):
        required = self.store.one("SELECT value FROM meta WHERE key='cloud_control_required'")
        release = self.store.one("SELECT value FROM meta WHERE key='meta_browser_cloud_catalog'")
        return bool(
            required
            and required["value"] == "1"
            and release
            and release["value"] == "pending_user_approval"
        )

    def sync(self):
        from .meta_migration import synchronize

        return synchronize(self)

    def backend(self, account, operation, payload):
        connection = self.connection(account["id"])
        observation = connection["observation"]
        verified = (
            fresh(observation, self.clock())
            and observation.get("identity") == connection["identity"]
            and observation.get("profile_url") == connection["profile_url"]
            and observation.get("browser") == connection["browser"]
            and observation.get("identity_verified") is True
        )
        if operation == "health":
            return {
                "healthy": verified,
                "service_ready": True,
                "identity_verified": verified,
                "account_identity": connection["identity"] if verified else None,
                "permissions": ["comment", "reply"] if verified else [],
                "reason": "meta_browser_identity_observed"
                if verified
                else "MetaBrowserRecheckRequired",
            }
        if operation == "preflight":
            self.sync()
            with self.store.connect() as db:
                reason = claim_blocker(
                    db, account, payload["payload"], payload.get("item_id", ""), self.clock()
                )
            # Only begin/guard can authorize a desktop submission.
            return {
                "allowed": False,
                "reason": reason
                or ("meta_browser_context_required" if verified else "MetaBrowserRecheckRequired"),
            }
        if operation == "verify":
            # Receipt collection is delegated to the same desktop browser session.
            return {
                "status": "unknown",
                "verified": False,
                "reason": "MetaBrowserReceiptCheckRequired",
            }
        raise BackendError("MetaBrowserDesktopExecutorRequired")

    def observe(self, account_id, proof):
        """Store a fresh observation from the existing authenticated browser."""
        connection = self.connection(account_id)
        if not (
            fresh(proof, self.clock())
            and proof.get("identity") == connection["identity"]
            and proof.get("profile_url") == connection["profile_url"]
            and proof.get("browser") == connection["browser"]
            and proof.get("identity_verified") is True
            and isinstance(proof.get("evidence"), str)
            and 0 < len(proof["evidence"]) <= 2000
        ):
            raise ValueError("需要当前浏览器的身份、主页、时间和核验依据")
        safe = {
            k: proof[k]
            for k in (
                "identity",
                "profile_url",
                "browser",
                "identity_verified",
                "observed_at",
                "evidence",
            )
        }
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE meta_browser_accounts SET observation=?,updated_at=? WHERE account_id=?",
                (canonical(safe), self.clock(), account_id),
            )
            # A new observation may resolve a previous expired-observation backoff.
            row = db.execute("SELECT health FROM accounts WHERE id=?", (account_id,)).fetchone()
            health = json.loads(row[0])
            health["next_check_at"] = 0
            db.execute("UPDATE accounts SET health=? WHERE id=?", (canonical(health), account_id))
        return self.engine.check_account(account_id, force=True)

    def status(self):
        self.sync()
        now = self.clock()
        connections = []
        for row in self.store.rows(
            "SELECT b.account_id FROM meta_browser_accounts b JOIN accounts a ON a.id=b.account_id "
            "WHERE a.platform IN ("
            + ",".join("?" for _ in self.platforms)
            + ") ORDER BY external_id",
            self.platforms,
        ):
            c = self.connection(row["account_id"])
            connections.append(
                {
                    "account_id": c["account_id"],
                    "label": self.store.one(
                        "SELECT label FROM accounts WHERE id=?", (c["account_id"],)
                    )["label"],
                    "external_id": c["external_id"],
                    "identity": c["identity"],
                    "profile_url": c["profile_url"],
                    "connector": c["connector"],
                    "browser": c["browser"],
                    "identity_observation_fresh": (
                        fresh(c["observation"], now)
                        and c["observation"].get("identity_verified") is True
                    ),
                    "blocked_reason": c["observation"].get("blocked_reason"),
                    "sources": self.store.rows(
                        "SELECT task_id,path,state,updated_at FROM meta_browser_sources WHERE account_id=?",
                        (c["account_id"],),
                    ),
                }
            )
            for source in connections[-1]["sources"]:
                source["coordination_only"] = source["path"] in {
                    str(Path(p).resolve()) for p in c["details"].get("coordination_paths", [])
                }
        return {
            "accounts": connections,
            "execution": "existing_desktop_browser",
            "requires_active_desktop_executor": True,
            "cloud_catalog_pending": self.release_pending(),
        }

    def pending(self):
        """Schedule, expire and inspect work; never claim or submit an item."""
        self.sync()
        queue = JobQueue(self.engine)
        queue.sync()
        self.engine.recover()
        now = self.clock()
        jobs = self.store.rows(
            "SELECT j.id job_id,j.reference_id item_id,j.task_id,j.account_id,j.available_at,"
            "j.expires_at,j.state,i.payload,i.payload_hash,a.label FROM execution_jobs j "
            "JOIN items i ON i.id=j.reference_id JOIN accounts a ON a.id=j.account_id "
            "JOIN tasks t ON t.id=j.task_id WHERE j.pool IN ("
            + ",".join("?" for _ in self.platforms)
            + ") "
            "AND j.state='pending' AND i.status='not_attempted' AND i.attempt_id IS NULL "
            "AND t.state='active' ORDER BY j.available_at,i.position LIMIT 100",
            tuple(p + ".desktop" for p in self.platforms),
        )
        for job in jobs:
            job["payload"] = json.loads(job["payload"])
            account = self.engine.account(job["account_id"])
            with self.store.connect() as db:
                job["blocker"] = claim_blocker(db, account, job["payload"], job["item_id"], now)
            job["due"] = job["available_at"] <= now < job["expires_at"]
            if self.release_pending():
                job["blocker"] = "meta_cloud_catalog_pending"
            job["connection"] = {
                k: self.connection(job["account_id"])[k]
                for k in ("identity", "profile_url", "connector", "browser")
            }
        unresolved = self.store.rows(
            "SELECT i.id item_id,i.task_id,i.account_id,i.status,i.external_reference,i.result,i.payload "
            "FROM items i JOIN meta_browser_accounts a ON a.account_id=i.account_id "
            "JOIN accounts ac ON ac.id=i.account_id "
            "LEFT JOIN meta_browser_checks c ON c.item_id=i.id "
            "WHERE ac.platform IN (" + ",".join("?" for _ in self.platforms) + ") "
            "AND i.status IN ('unknown','scheduled') AND COALESCE(c.checked_at,0)<=? ORDER BY i.due_at LIMIT 30",
            (*self.platforms, now - 10800),
        )
        for row in unresolved:
            row["result"] = json.loads(row["result"])
            row["payload"] = json.loads(row["payload"])
            row["connection"] = {
                k: self.connection(row["account_id"])[k]
                for k in ("identity", "profile_url", "connector", "browser")
            }
            owner = self.store.one(
                "SELECT owner FROM meta_browser_claims WHERE item_id=?", (row["item_id"],)
            )
            row["owner"] = owner["owner"] if owner else None
        feedback_jobs = self.store.rows(
            "SELECT i.id item_id,i.account_id,i.external_reference,i.payload,i.claimed_at "
            "FROM items i JOIN meta_browser_claims m ON m.item_id=i.id "
            "JOIN accounts ac ON ac.id=i.account_id "
            "LEFT JOIN feedback_checks f ON f.item_id=i.id WHERE i.status='confirmed' "
            "AND ac.platform IN (" + ",".join("?" for _ in self.platforms) + ") "
            "AND i.claimed_at>=? AND COALESCE(f.checked_through,0)<=? ORDER BY i.claimed_at LIMIT 20",
            (*self.platforms, now - 86400, now - 10800),
        )
        for row in feedback_jobs:
            row["payload"] = json.loads(row["payload"])
            row["observation_ends_at"] = row["claimed_at"] + 86400
            row["connection"] = {
                k: self.connection(row["account_id"])[k]
                for k in ("identity", "profile_url", "connector", "browser")
            }
        return {
            "jobs": jobs,
            "unresolved": unresolved,
            "feedback_jobs": feedback_jobs,
            "unverified_history": self.store.one(
                "SELECT count(*) n FROM meta_browser_history h JOIN items i ON i.id=h.item_id "
                "JOIN accounts a ON a.id=i.account_id WHERE i.status='manual_attention' "
                "AND a.platform IN (" + ",".join("?" for _ in self.platforms) + ")",
                self.platforms,
            )["n"],
        }

    def begin(self, item_id, proof):
        """Lease the normal job and create its one attempt before any UI submission."""
        from .core import Conflict
        from .task_channels import TaskChannelRouter

        if self.release_pending():
            raise Conflict("Facebook／IG 的线上平台目录尚待发布批准，保持未尝试")

        self.sync()
        item = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not item or item["status"] != "not_attempted" or item["attempt_id"]:
            raise Conflict("该条已尝试或不再等待执行，只能核对回执")
        if self.store.one("SELECT 1 FROM meta_browser_history WHERE item_id=?", (item_id,)):
            raise Conflict("迁入的旧条目只作历史记录，不进入发送队列")
        account = self.engine.account(item["account_id"])
        connection = self.connection(account["id"])
        payload = json.loads(item["payload"])
        if not (
            fresh(proof, self.clock())
            and proof.get("identity") == connection["identity"]
            and proof.get("browser") == connection["browser"]
            and proof.get("payload_hash") == item["payload_hash"]
            and proof.get("target") == payload["target"]
            and all(
                proof.get(k) is True
                for k in ("identity_verified", "context_verified", "can_reply", "no_duplicate")
            )
            and isinstance(proof.get("source_quote"), str)
            and proof["source_quote"].strip()
            and isinstance(proof.get("evidence"), str)
            and 0 < len(proof["evidence"]) <= 4000
        ):
            raise Conflict("提交前需要当前身份、原帖上下文、回复权限及无重复的核验依据")
        health = self.backend(account, "health", {})
        if not health["healthy"]:
            raise Conflict("请先在原浏览器核验身份并登记新观察")
        task = self.engine.task(item["task_id"])
        if task["policy"].get("execution_channel") != "local":
            raise Conflict("现有桌面浏览器任务需要明确使用本机通道")
        if not self.engine.live_enabled or not self.engine.adapters.commissioned(account):
            raise Conflict("当前真实互动执行开关未允许该平台")
        queue = JobQueue(self.engine)
        queue.sync(task["id"])
        owner = "meta-desktop-" + uuid.uuid4().hex
        job = queue.claim(
            route_for(account, payload).pool, owner, LEASE_SECONDS, reference_id=item_id
        )
        if not job:
            raise Conflict("任务尚未到期、已停止、账号处于等待期或浏览器正在使用")
        claimed = None
        from .meta_coordination import LegacyCoordination

        coordination = LegacyCoordination(connection)
        lock_owned = False
        try:
            coordination.acquire(owner, min(self.clock() + LEASE_SECONDS, job["expires_at"]))
            lock_owned = True
            context, _ = TaskChannelRouter(self.engine).prepare(job)
            with job_scope(job), self.engine.adapters.execution_job(context):
                claimed = self.engine._claim(item_id, {"allowed": True})
            if not claimed:
                raise Conflict("统一任务、节奏、额度或历史状态检查未通过")
            expires = min(self.clock() + LEASE_SECONDS, job["expires_at"])
            with self.store.connect(write=True) as db:
                route = json.loads(job["route"])
                if route.get("resource"):
                    occupied = db.execute(
                        "SELECT 1 FROM execution_resources WHERE resource=? AND expires_at>?",
                        (route["resource"], self.clock()),
                    ).fetchone()
                    if occupied:
                        raise Conflict("浏览器已被其他账号占用")
                    db.execute(
                        "INSERT INTO execution_resources VALUES(?,?,?) ON CONFLICT(resource) "
                        "DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
                        (route["resource"], owner, expires),
                    )
                db.execute(
                    "INSERT INTO account_lanes VALUES(?,?,?,?) ON CONFLICT(account_id) "
                    "DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at,"
                    "heartbeat_at=excluded.heartbeat_at",
                    (account["id"], owner, expires, self.clock()),
                )
                safe_proof = {
                    k: proof[k]
                    for k in (
                        "identity",
                        "browser",
                        "payload_hash",
                        "target",
                        "identity_verified",
                        "context_verified",
                        "can_reply",
                        "no_duplicate",
                        "source_quote",
                        "evidence",
                        "observed_at",
                    )
                }
                safe_proof.update(self.extra_proof(payload, proof))
                db.execute(
                    "INSERT INTO meta_browser_claims(item_id,owner,job,context,claim,proof,expires_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        item_id,
                        owner,
                        canonical(job),
                        canonical(context),
                        canonical(claimed),
                        canonical(safe_proof),
                        expires,
                    ),
                )
                key = event_key(account["platform"], payload["target"], payload)
                db.execute(
                    "INSERT OR IGNORE INTO meta_browser_contacts VALUES(?,?,?,?,?)",
                    (account["platform"], key, item_id, account["id"], "executing"),
                )
                Store.audit(
                    db, "meta_browser.claimed", {"item_id": item_id}, task["id"], self.clock()
                )
            from .meta_migration import sample_next

            next_at = sample_next(self, account["id"], self.clock())
            coordination.update(
                owner,
                {
                    "owner": owner,
                    "status": "executing",
                    "item_id": item_id,
                    "task_id": task["id"],
                    "source": "auto_promotion",
                },
                next_at=next_at,
            )
            return {
                "item_id": item_id,
                "owner": owner,
                "expires_at": expires,
                "payload": payload,
                "instruction": "重读当前上下文后调用 guard；guard 成功后只提交一次，再独立核对回执",
            }
        except Exception:
            if claimed:
                self.engine.finish(
                    claimed,
                    {
                        "status": "failed_known",
                        "verified": False,
                        "confirmed_not_submitted": True,
                        "reason": "MetaDesktopClaimSetupFailed",
                    },
                )
            if lock_owned:
                coordination.release(owner)
            queue.settle(job)
            raise

    def guard(self, item_id, owner):
        """The immediate pre-click guard. A second call never authorizes a replay."""
        from .core import Conflict

        row = self.store.one(
            "SELECT * FROM meta_browser_claims WHERE item_id=? AND owner=?", (item_id, owner)
        )
        if not row or row["expires_at"] <= self.clock() or row["submission_started"]:
            raise Conflict("提交许可已用过或已过期；只核对该条回执")
        claim = json.loads(row["claim"])
        account = self.engine.account(claim["payload"]["account_id"])
        self.sync()
        proof = json.loads(row["proof"])
        if not fresh(proof, self.clock()) or not self.backend(account, "health", {})["healthy"]:
            raise Conflict("当前身份或上下文观察已过期")
        from .meta_coordination import LegacyCoordination

        coordination = LegacyCoordination(self.connection(account["id"]))
        if coordination.lock and (
            not coordination.lock.is_file()
            or json.loads(coordination.lock.read_text(encoding="utf-8-sig")).get("owner") != owner
        ):
            raise Conflict("IG 共享互动锁已变化，不再允许提交")
        with self.store.connect() as db:
            reason = claim_blocker(db, account, claim["payload"], item_id, self.clock())
        if reason:
            raise Conflict(reason)
        verify_claim(
            {
                "platform": account["platform"],
                "account": account["backend_label"],
                "account_id": account["id"],
                "database": self.store.path,
                "operation": "execute",
                "execution_job": json.loads(row["context"]),
                "payload": claim,
            }
        )
        with self.store.connect(write=True) as db:
            guarded_at = self.clock()
            if account["platform"] == "facebook":
                claim_result = json.loads(row["result"])
                claim_result["submission_guarded_at"] = guarded_at
                changed = db.execute(
                    "UPDATE meta_browser_claims SET submission_started=1,result=? WHERE item_id=? "
                    "AND owner=? AND submission_started=0 AND expires_at>?",
                    (canonical(claim_result), item_id, owner, guarded_at),
                ).rowcount
            else:
                changed = db.execute(
                    "UPDATE meta_browser_claims SET submission_started=1 WHERE item_id=? "
                    "AND owner=? AND submission_started=0 AND expires_at>?",
                    (item_id, owner, guarded_at),
                ).rowcount
            if not changed:
                raise Conflict("该条提交许可已被使用")
            db.execute(
                "UPDATE items SET result=? WHERE id=? AND status='executing'",
                (
                    canonical(
                        {"execution_step": "meta_browser_submit", "submission_started": True}
                    ),
                    item_id,
                ),
            )
        return {"allowed": True, "item_id": item_id, "submit_once": True}

    def result(self, item_id, receipt, owner=None):
        """Persist an independently read receipt; an unknown outcome stays nonretryable."""
        from .core import Conflict

        item = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not item or item["status"] not in {
            "executing",
            "unknown",
            "confirmed",
            "manual_attention",
            "cancelled",
        }:
            raise Conflict("该条没有待核对的提交")
        row = self.store.one("SELECT * FROM meta_browser_claims WHERE item_id=?", (item_id,))
        if row and owner != row["owner"]:
            raise Conflict("执行归属不匹配")
        if not row and not self.store.one(
            "SELECT 1 FROM meta_browser_history WHERE item_id=?", (item_id,)
        ):
            raise Conflict("该条不属于 Meta 浏览器任务")
        account = self.engine.account(item["account_id"])
        connection = self.connection(account["id"])
        payload = json.loads(item["payload"])
        status = receipt.get("status", "unknown")
        result = {"status": "unknown", "verified": False, "reason": "MetaBrowserReceiptUnresolved"}
        moderation_observation = None
        previous_result = json.loads(item["result"])
        supports_skipped_confirmation = account["platform"] == "facebook" or (
            account["platform"] == "tiktok"
            and payload.get("action") in {"send_direct_message", "reply_direct_message"}
        )
        skipped_unknown = bool(
            supports_skipped_confirmation
            and row
            and item["status"] == "cancelled"
            and previous_result.get("reason") == "UserSkippedUnknownSubmission"
        )
        if status == "confirmed":
            if (
                account["platform"] in {"facebook", "tiktok"}
                and item["status"] == "cancelled"
                and not skipped_unknown
            ):
                raise Conflict("只有用户跳过的未知提交可以接收后续独立确认回执")
            if row and not row["submission_started"]:
                raise Conflict("尚未登记提交步骤，不能把该条计为发布成功")
            reference = receipt.get("external_reference", "")
            same_target = self.same_receipt_target(account, payload, reference, receipt)
            is_comment = self.receipt_target(account, payload, reference, receipt)
            if not (
                fresh(receipt, self.clock())
                and same_target
                and is_comment
                and receipt.get("identity") == connection["identity"]
                and receipt.get("observed_text") == payload["text"]
                and receipt.get("parent_target") == payload["target"]
                and receipt.get("author_verified") is True
                and receipt.get("parent_verified") is True
                and receipt.get("verified") is True
                and receipt.get("evidence")
                and (
                    receipt.get("published_at") is None
                    or publication_time(receipt["published_at"], self.clock()) is not None
                )
            ):
                raise Conflict(
                    "成功回执需要独立读取作者、完整正文、父评论和评论链接；发布时间缺失时留空"
                )
            result = {
                "status": "confirmed",
                "verified": True,
                "external_reference": reference,
                "published_at": receipt.get("published_at"),
            }
            result.update(self.receipt_details(payload, receipt))
        elif status == "pending_admin_approval" and account["platform"] == "facebook":
            guarded_at = json.loads(row["result"]).get("submission_guarded_at") if row else None
            if not (
                account["platform"] == "facebook"
                and item["status"] in {"executing", "unknown"}
                and row
                and row["submission_started"] == 1
                and fresh(receipt, self.clock())
                and type(guarded_at) in {int, float}
                and math.isfinite(guarded_at)
                and receipt.get("observed_at", 0) >= guarded_at
                and receipt.get("identity") == connection["identity"]
                and receipt.get("identity_verified") is True
                and receipt.get("author_verified") is True
                and receipt.get("parent_verified") is True
                and receipt.get("independent_view") is True
                and receipt.get("parent_target") == payload["target"]
                and receipt.get("observed_url") == payload["target"]
                and receipt.get("observed_text") == payload["text"]
                and receipt.get("moderation_label") in MODERATION_LABELS
                and receipt.get("verified") is False
                and not receipt.get("external_reference")
                and isinstance(receipt.get("evidence"), str)
                and 0 < len(receipt["evidence"]) <= 2000
            ):
                raise Conflict("待审核需要独立核对账号、作者、全文、父目标及平台审核提示")
            moderation_observation = {
                k: receipt[k]
                for k in (
                    "identity",
                    "identity_verified",
                    "author_verified",
                    "parent_verified",
                    "independent_view",
                    "parent_target",
                    "observed_url",
                    "observed_text",
                    "moderation_label",
                    "observed_at",
                    "evidence",
                )
            }
            moderation_observation.update(
                item_id=item_id,
                attempt_id=item["attempt_id"],
                payload_hash=item["payload_hash"],
                account_id=item["account_id"],
            )
            result = {
                "status": "unknown",
                "verified": False,
                "platform_status": "pending_admin_approval",
                "reason": "MetaBrowserPendingGroupApproval",
            }
        elif status == "cancelled":
            previous = json.loads(item["result"])
            if not (
                row
                and row["submission_started"]
                and (
                    item["status"] == "unknown"
                    or (
                        item["status"] == "cancelled"
                        and previous.get("reason") == "UserSkippedUnknownSubmission"
                    )
                )
                and receipt.get("acknowledged_unknown") is True
                and isinstance(receipt.get("authorization"), str)
                and 0 < len(receipt["authorization"].strip()) <= 2000
                and receipt.get("evidence")
            ):
                raise Conflict("跳过未知结果需要明确用户指令和原执行归属；不代表确认未发送")
            result = {
                "status": "cancelled",
                "verified": False,
                "reason": "UserSkippedUnknownSubmission",
            }
        elif status == "failed_known":
            if (
                not row
                or row["submission_started"]
                or receipt.get("confirmed_not_submitted") is not True
            ):
                raise Conflict("已进入提交步骤的结果必须保留未知，等待只读核验")
            result = {
                "status": "failed_known",
                "verified": False,
                "confirmed_not_submitted": True,
                "reason": "MetaBrowserNotSubmitted",
            }
        if item["status"] == "confirmed":
            previous = json.loads(item["result"])
            self._legacy_result(row, item, previous)
            return previous
        claim = (
            json.loads(row["claim"])
            if row
            else {
                "item_id": item_id,
                "attempt_id": item["attempt_id"],
                "task_id": item["task_id"],
                "payload": payload,
            }
        )
        with self.store.connect(write=True) as db:
            if status == "unknown" and item["status"] == "unknown" and row:
                existing = db.execute(
                    "SELECT i.id,i.account_id,i.status,i.payload,i.payload_hash,i.attempt_id,"
                    "i.result item_result,c.claim,c.result claim_result,c.submission_started "
                    "FROM items i JOIN meta_browser_claims c ON c.item_id=i.id WHERE i.id=?",
                    (item_id,),
                ).fetchone()
                if existing and verified_pending_moderation(
                    existing, account, connection, self.clock()
                ):
                    moderation_observation = json.loads(existing["claim_result"])[
                        "moderation_observation"
                    ]
                    result.update(
                        platform_status="pending_admin_approval",
                        reason="MetaBrowserPendingGroupApproval",
                    )
            if result["status"] == "confirmed":
                self.persist_receipt(db, item, result, receipt)
            db.execute(
                "INSERT INTO meta_browser_checks VALUES(?,?) ON CONFLICT(item_id) DO UPDATE SET checked_at=excluded.checked_at",
                (item_id, self.clock()),
            )
            if row:
                normalized = self.engine.finish(
                    claim,
                    result,
                    _db=db,
                    _allow_skipped_confirmation=skipped_unknown and result["status"] == "confirmed",
                )
            else:
                from .statistics import record_publication

                normalized = result
                db.execute(
                    "UPDATE items SET status=?,external_reference=COALESCE(?,external_reference),result=? WHERE id=?",
                    (
                        result["status"],
                        result.get("external_reference"),
                        canonical(result),
                        item_id,
                    ),
                )
                if result["status"] == "confirmed":
                    record_publication(db, item_id, result, self.clock())
            claim_result = dict(normalized)
            if row:
                guarded_at = json.loads(row["result"]).get("submission_guarded_at")
                if type(guarded_at) in {int, float} and math.isfinite(guarded_at):
                    claim_result["submission_guarded_at"] = guarded_at
            if moderation_observation and normalized.get("status") == "unknown":
                claim_result["moderation_observation"] = moderation_observation
            db.execute(
                "UPDATE meta_browser_claims SET result=? WHERE item_id=?",
                (canonical(claim_result), item_id),
            )
            if row:
                job = json.loads(row["job"])
                db.execute(
                    "UPDATE execution_jobs SET state=?,owner=NULL,lease_until=0,updated_at=? "
                    "WHERE id=? AND owner=?",
                    (normalized["status"], self.clock(), job["id"], owner),
                )
                if skipped_unknown and normalized.get("status") == "confirmed":
                    db.execute(
                        "UPDATE execution_jobs SET state='confirmed',updated_at=? "
                        "WHERE id=? AND kind='send' AND reference_id=? AND task_id=? "
                        "AND account_id=? AND version=? AND owner IS NULL AND state IN ('unknown','cancelled')",
                        (
                            self.clock(),
                            job["id"],
                            item_id,
                            item["task_id"],
                            item["account_id"],
                            item["payload_hash"],
                        ),
                    )
                db.execute("DELETE FROM execution_resources WHERE owner=?", (owner,))
                db.execute("DELETE FROM account_lanes WHERE owner=?", (owner,))
            self.engine._mark_completed(db, item["task_id"])
            db.execute(
                "UPDATE meta_browser_contacts SET state=? WHERE item_id=?",
                (normalized["status"], item_id),
            )
            Store.audit(
                db,
                "meta_browser.receipt",
                {
                    "item_id": item_id,
                    "status": normalized["status"],
                    "evidence": str(receipt.get("evidence", ""))[:2000],
                },
                item["task_id"],
                self.clock(),
            )
            if (
                result["status"] == "cancelled"
                and normalized.get("reason") == "UserSkippedUnknownSubmission"
            ):
                Store.audit(
                    db,
                    "meta_browser.user_skipped_unknown",
                    {
                        "item_id": item_id,
                        "owner": owner,
                        "authorization": receipt["authorization"],
                        "prior_status": item["status"],
                        "prior_result": json.loads(item["result"]),
                        "submission_outcome": "unknown",
                        "target_reservation_retained": True,
                    },
                    item["task_id"],
                    self.clock(),
                )
            if skipped_unknown and normalized.get("status") == "confirmed":
                Store.audit(
                    db,
                    "meta_browser.skipped_submission_later_verified",
                    {
                        "item_id": item_id,
                        "prior_status": "cancelled",
                        "external_reference": normalized.get("external_reference"),
                    },
                    item["task_id"],
                    self.clock(),
                )
        self._legacy_result(row, item, normalized)
        return normalized

    def extra_proof(self, payload, proof):
        return {}

    def same_receipt_target(self, account, payload, reference, receipt):
        try:
            return event_key(account["platform"], reference) == event_key(
                account["platform"], payload["target"]
            )
        except ValueError:
            return False

    def receipt_details(self, payload, receipt):
        return {}

    def persist_receipt(self, db, item, result, receipt):
        return

    def receipt_target(self, account, payload, reference, receipt):
        parsed = urlparse(reference)
        if account["platform"] == "instagram":
            return bool(re.search(r"/c/[0-9]+", parsed.path))
        query = parse_qs(parsed.query)
        comment = query.get("comment_id", [""])[0]
        reply = query.get("reply_comment_id", [""])[0]
        target_query = parse_qs(urlparse(payload["target"]).query)
        if len(query.get("comment_id", [])) != 1 or len(query.get("reply_comment_id", [])) > 1:
            return False
        parent = target_query.get("comment_id", [""])[0]
        parent_reply = target_query.get("reply_comment_id", [""])[0]
        if not re.fullmatch(r"[0-9]+", comment):
            return False
        if parent:
            return bool(
                comment == parent
                and re.fullmatch(r"[0-9]+", reply)
                and reply not in {parent, parent_reply}
            )
        return not reply or bool(re.fullmatch(r"[0-9]+", reply))

    def _legacy_result(self, claim_row, item, result):
        """Recoverable mirror; a failed mirror never causes another submission."""
        if not claim_row:
            return
        from .meta_coordination import LegacyCoordination
        from .meta_migration import iso

        connection = self.connection(item["account_id"])
        coordination = LegacyCoordination(connection)
        if not coordination.path:
            return
        state = json.loads(coordination.path.read_text(encoding="utf-8-sig"))
        inflight = state.get("coordination_inflight") or {}
        owner = claim_row["owner"]
        if inflight.get("owner") != owner:
            return
        if not coordination.lock.is_file():
            coordination.acquire(owner, self.clock() + LEASE_SECONDS)
        folder = Path(self.store.path).parent / "meta-browser-receipts"
        folder.mkdir(exist_ok=True)
        receipt_path = folder / (item["id"] + ".json")
        receipt_path.write_text(
            canonical(
                {
                    **result,
                    "account": connection["identity"],
                    "task_id": item["task_id"],
                    "item_id": item["id"],
                    "reply_url": result.get("external_reference"),
                    "sent_at": iso(result["published_at"]) if result.get("published_at") else None,
                }
            ),
            encoding="utf-8",
        )
        coordination.update(
            owner,
            {
                "owner": owner,
                "source": "auto_promotion",
                "identity": connection["identity"],
                "item_id": item["id"],
                "task_id": item["task_id"],
                **result,
            },
            next_at=self.engine.account(item["account_id"])["next_at"],
            receipt_path=receipt_path,
        )
        coordination.release(owner)

    def feedback(self, item_id, evidence):
        """Read-only reply observations use the common statistics and inbox tables."""
        item = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not item or item["status"] != "confirmed":
            raise ValueError("回复观察需要已核验的互动")
        connection = self.connection(item["account_id"])
        if not (
            fresh(evidence, self.clock())
            and evidence.get("identity") == connection["identity"]
            and evidence.get("external_reference") == item["external_reference"]
            and evidence.get("evidence")
            and evidence.get("coverage") in {"partial", "complete"}
        ):
            raise ValueError("需要当前评论线程的身份、回执链接、观察时间和覆盖依据")
        statistics = Statistics(self.store, clock=self.clock)
        events = evidence.get("events", [])
        if not isinstance(events, list) or len(events) > 500:
            raise ValueError("单次回复观察最多 500 条")
        for event in events:
            if event.get("direct_reply") is not True:
                raise ValueError("仅记录他人对本条互动的直接回复")
            statistics.record_feedback(
                item_id,
                str(event["event_id"]),
                event["occurred_at"],
                is_self=event.get("is_self") is True,
            )
        statistics.mark_feedback(item_id, evidence["observed_at"], evidence["coverage"])
        return {
            "item_id": item_id,
            "observed_events": len(events),
            "coverage": evidence["coverage"],
        }

"""Reusable task templates and task-scoped, credential-free evidence packages."""

import csv
import hashlib
import html
import io
import json
import math
import re
import uuid
import zipfile
from collections import Counter
from datetime import datetime

from .account_blocks import FIELDS as BLOCK_FIELDS
from .core import Conflict, NotFound
from .execution_diagnostics import safe_code
from .models import Policy, TaskInput, canonical
from .read_cooldowns import FIELDS as X_READ_FIELDS
from .statistics import ZONE
from .store import Store, decode
from .telegram_reading import READ_FIELDS

ACCOUNT_FIELDS = (
    "id",
    "platform",
    "label",
    "mode",
    "daily_limit",
    "hourly_limit",
    "min_interval",
    "outbound_block",
)
ITEM_FIELDS = (
    "account_id",
    "account_label",
    "platform",
    "position",
    "action",
    "target",
    "text",
    "title",
    "scheduled_at",
    "reply_to",
    "discord_reply_to",
    "recipient_id",
    "target_owned",
    "execution_mode",
    "opportunity_id",
    "content_key",
    "profile_version",
    "tiktok_video_id",
    "tiktok_comment_id",
    "tiktok_conversation_id",
    "tiktok_message_id",
    "tiktok_target_evidence",
    "tiktok_options",
)
RESULT_FIELDS = (
    (
        "status",
        "verified",
        "external_reference",
        "scheduled",
        "active",
        "published_at",
        "message_ids",
        "message_chat_id",
        "scheduled_message_ids",
        "lookup_message_ids",
        "can_send",
        "outbound_eligible_at_utc",
        "retry_after_seconds",
        "step_started_at",
        "submission_started",
        "skipped_at",
        "skip_reason",
        "publish_id",
        "post_ids",
        "comment_id",
        "message_id",
        "conversation_id",
        "recipient_id",
        "interaction_channel",
        "tiktok_receipt_binding",
        "platform_status",
    )
    + READ_FIELDS
    + X_READ_FIELDS
)
AUDIT_FIELDS = (
    "channel",
    "job_id",
    "generation",
    "retry_at",
    "fence_source",
    "source",
    "item_id",
    "attempt_id",
    "candidate_id",
    "account_id",
    "stage",
    "actor",
    "hash",
    "policy_hash",
    "manifest_hash",
    "count",
    "status",
    "mode",
    "state",
    "phase",
    "verified",
    "external_reference",
    "until",
    "stopped",
    "action",
    "target",
    "published_at",
    "worker_id",
    "pid",
    "exit_code",
    "submission_started",
    "approved_at",
    "template_id",
)


def pick(value, fields):
    value = dict(value)
    return {key: value[key] for key in fields if key in value}


def diagnostics(value):
    result = {}
    for field in ("reason", "error_code", "execution_step", "exception_type", "skip_reason"):
        if field in value and isinstance(value[field], str):
            result[field] = safe_code(value[field], "details_omitted")
    frames = []
    trace = value.get("diagnostic_traceback", [])
    for frame in trace[-12:] if isinstance(trace, list) else []:
        if not isinstance(frame, dict):
            continue
        path = frame.get("file", "")
        if not isinstance(path, str) or ".." in path or ":" in path or path.startswith(("/", "\\")):
            continue
        line = frame.get("line")
        if isinstance(line, int) and line > 0:
            frames.append(
                {"file": path, "function": safe_code(frame.get("function")), "line": line}
            )
    if frames:
        result["diagnostic_traceback"] = frames
    return result


def safe_result(value):
    result = {
        **pick(
            value, tuple(key for key in RESULT_FIELDS if key not in READ_FIELDS + X_READ_FIELDS)
        ),
        **diagnostics(value),
    }
    for key in READ_FIELDS + X_READ_FIELDS:
        if key not in value or key == "read_cursor_next":
            continue
        entry = value[key]
        if key == "read_resource_failures" and isinstance(entry, dict):
            result[key] = {
                safe_code(name, "resource_error"): count
                for name, count in entry.items()
                if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 1000000
            }
        elif key in {"read_identity_hash", "read_page_fingerprint"}:
            if isinstance(entry, str) and re.fullmatch(r"[a-f0-9]{64}", entry):
                result[key] = entry
        elif key == "read_peer_id":
            if isinstance(entry, str) and re.fullmatch(r"-?[1-9][0-9]{0,19}", entry):
                result[key] = entry
        elif entry is None or isinstance(entry, (bool, int)):
            result[key] = entry
        elif isinstance(entry, float) and math.isfinite(entry):
            result[key] = entry
        elif isinstance(entry, str):
            result[key] = safe_code(entry, "details_omitted")
    for field in BLOCK_FIELDS:
        if field not in value:
            continue
        entry = value[field]
        if field == "rpc_counts":
            if isinstance(entry, dict):
                result[field] = {
                    key: count
                    for key, count in entry.items()
                    if key
                    in {
                        "mutation",
                        "identity",
                        "permissions",
                        "discovery",
                        "history",
                        "entity",
                        "other",
                    }
                    and isinstance(count, int)
                    and not isinstance(count, bool)
                    and count >= 0
                }
        elif field in {
            "runtime_git_commit_at_start",
            "runtime_source_fingerprint",
            "account_block_event_id",
        }:
            if entry is None or isinstance(entry, str) and re.fullmatch(r"[a-f0-9]{32,64}", entry):
                result[field] = entry
        elif isinstance(entry, str):
            result[field] = safe_code(entry, "details_omitted")
        elif entry is None or isinstance(entry, (bool, int, float)):
            result[field] = entry
    return result


def item_payload(value):
    result = pick(value, ITEM_FIELDS)
    result["media"] = [
        pick(media, ("filename", "sha256", "mime_type", "media_type", "is_ai_generated"))
        for media in value.get("media", [])
    ]
    return result


class TaskTools:
    def __init__(self, engine):
        self.engine, self.store, self.clock = engine, engine.store, engine.clock

    def templates(self):
        return [
            decode(row, "settings", "platforms")
            for row in self.store.rows("SELECT * FROM task_templates ORDER BY created_at DESC,id")
        ]

    def template(self, template_id):
        row = self.store.one("SELECT * FROM task_templates WHERE id=?", (template_id,))
        if not row:
            raise NotFound("任务模板不存在")
        return decode(row, "settings", "platforms")

    def save_template(self, value):
        task = self.engine.task(value.source_task_id)
        settings = Policy.model_validate(task["policy"]).model_dump(
            exclude={
                "starts_at",
                "ends_at",
                "target_allowlist",
                "quota_snapshot",
                "outbound_after_task_ids",
                "outbound_after_tasks_seconds",
            }
        )
        template_id = "tpl-" + uuid.uuid4().hex[:16]
        platforms = sorted({a["platform"] for a in task["account_snapshot"]})
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO task_templates VALUES(?,?,?,?,?,?,?,?)",
                (
                    template_id,
                    value.name,
                    value.description,
                    task["mode"],
                    canonical(platforms),
                    canonical(settings),
                    task["id"],
                    self.clock(),
                ),
            )
            Store.audit(
                db, "template.saved", {"template_id": template_id}, task["id"], self.clock()
            )
        return self.template(template_id)

    def create_from_template(self, template_id, value):
        template = self.template(template_id)
        accounts = [self.engine.account(identifier) for identifier in value.account_ids]
        if any(
            a["mode"] != template["mode"] or a["platform"] not in template["platforms"]
            for a in accounts
        ):
            raise Conflict("请选择模板对应平台和真实／模拟模式的账号")
        policy = {
            **template["settings"],
            "starts_at": value.starts_at,
            "ends_at": value.ends_at,
            "target_allowlist": value.target_allowlist,
        }
        if value.execution_channel is not None:
            policy["execution_channel"] = value.execution_channel
        # Engine captures current quotas and creates a fresh ID with no approvals or items.
        return self.engine.create_task(
            TaskInput(name=value.name, account_ids=value.account_ids, policy=policy)
        )

    def evidence(self, task_id):
        """Take one consistent read snapshot; never query profiles or account health."""
        with self.store.connect() as db:
            db.execute("BEGIN")
            raw = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not raw:
                raise NotFound("任务不存在")
            task = pick(
                raw,
                (
                    "id",
                    "name",
                    "mode",
                    "state",
                    "phase",
                    "policy_hash",
                    "manifest_hash",
                    "created_at",
                    "updated_at",
                    "next_at",
                ),
            )
            task["policy"] = json.loads(raw["policy"])
            task["accounts"] = [
                pick(a, ACCOUNT_FIELDS) for a in json.loads(raw["account_snapshot"])
            ]
            task["manifest_frozen"] = bool(raw["manifest"])
            approvals = [
                dict(row)
                for row in db.execute(
                    "SELECT stage,hash,phrase,actor,approved_at FROM approvals WHERE task_id=? ORDER BY approved_at",
                    (task_id,),
                )
            ]
            items = []
            for row in db.execute(
                "SELECT * FROM items WHERE task_id=? ORDER BY position", (task_id,)
            ):
                item = pick(
                    row,
                    (
                        "id",
                        "account_id",
                        "position",
                        "payload_hash",
                        "due_at",
                        "status",
                        "attempt_id",
                        "claimed_at",
                        "external_reference",
                    ),
                )
                item["payload"] = item_payload(json.loads(row["payload"]))
                item["result"] = safe_result(json.loads(row["result"]))
                items.append(item)
            candidates = []
            for row in db.execute(
                "SELECT * FROM candidates WHERE task_id=? ORDER BY id", (task_id,)
            ):
                candidate = pick(row, ("id", "account_id", "target", "decision", "state"))
                candidate["payload"] = pick(
                    json.loads(row["payload"]),
                    (
                        "account_id",
                        "target",
                        "title",
                        "text",
                        "language",
                        "members",
                        "activity",
                        "confidence",
                        "is_public",
                    ),
                )
                candidate["reasons"] = json.loads(row["reasons"])
                candidate["result"] = safe_result(json.loads(row["result"]))
                candidates.append(candidate)

            def rows(sql):
                return [dict(row) for row in db.execute(sql, (task_id,))]

            attempts = rows(
                "SELECT id,item_id,account_id,mode,started_at,finished_at,outcome FROM attempts WHERE task_id=? ORDER BY started_at"
            )
            joins = rows(
                "SELECT id,candidate_id,account_id,mode,started_at,finished_at,outcome FROM join_attempts WHERE task_id=? ORDER BY started_at"
            )
            calls = rows(
                "SELECT id,at,platform,operation,status,duration_ms,error_code,execution_step FROM calls WHERE task_id=? ORDER BY id"
            )
            for call in calls:
                call.update(diagnostics(call))
            audit = []
            for row in db.execute(
                "SELECT id,at,kind,details FROM audit WHERE task_id=? ORDER BY id", (task_id,)
            ):
                details = json.loads(row["details"])
                allowed = {
                    key: val
                    for key, val in pick(details, AUDIT_FIELDS).items()
                    if isinstance(val, (str, int, float, bool)) or val is None
                }
                audit.append(
                    {
                        "id": row["id"],
                        "at": row["at"],
                        "kind": row["kind"],
                        "details": {**allowed, **diagnostics(details)},
                    }
                )
            workers = rows(
                "SELECT id,pid,live_enabled,started_at,heartbeat_at,finished_at,state,phase,reason,exit_code,diagnostics FROM worker_runs WHERE task_id=? ORDER BY started_at"
            )
            for worker in workers:
                worker["diagnostics"] = diagnostics(json.loads(worker["diagnostics"]))
                if worker["reason"]:
                    worker["reason"] = safe_code(worker["reason"], "details_omitted")
            publications = rows(
                "SELECT p.* FROM publication_evidence p JOIN items i ON i.id=p.item_id WHERE i.task_id=?"
            )
            feedback = rows(
                "SELECT f.* FROM feedback_events f JOIN items i ON i.id=f.item_id WHERE i.task_id=? ORDER BY occurred_at"
            )
            coverage = rows(
                "SELECT f.* FROM feedback_checks f JOIN items i ON i.id=f.item_id WHERE i.task_id=?"
            )
        from .account_blocks import active_block

        with self.store.connect() as db:
            account_blocks = [
                {"account_id": account_id, **block}
                for account_id in json.loads(raw["account_ids"])
                if (block := active_block(db, account_id, self.clock()))
            ]
        return {
            "schema_version": 1,
            "exported_at": datetime.fromtimestamp(self.clock(), ZONE).isoformat(),
            "timezone": "Asia/Shanghai",
            "task": task,
            "approvals": approvals,
            "items": items,
            "candidates": candidates,
            "attempts": attempts,
            "join_attempts": joins,
            "calls": calls,
            "audit": audit,
            "workers": workers,
            "publication_evidence": publications,
            "feedback_events": feedback,
            "feedback_checks": coverage,
            "account_blocks": account_blocks,
            "execution_read_observations": [
                {
                    "account_id": row["account_id"],
                    "operation": row["operation"],
                    "at": row["at"],
                    "result": safe_result(json.loads(row["result"])),
                }
                for row in self.store.rows(
                    "SELECT * FROM execution_read_observations WHERE (task_id=? OR task_id IS NULL) AND account_id IN (SELECT value FROM json_each(?)) ORDER BY id DESC LIMIT 1000",
                    (task_id, raw["account_ids"]),
                )
            ],
            "telegram_read_observations": [
                {
                    "account_id": row["account_id"],
                    "operation": row["operation"],
                    "observed_at": row["observed_at"],
                    "result": safe_result(json.loads(row["result"])),
                }
                for row in self.store.rows(
                    "SELECT * FROM telegram_read_observations WHERE (task_id=? OR task_id IS NULL) AND account_id IN (SELECT value FROM json_each(?)) ORDER BY id DESC LIMIT 1000",
                    (task_id, raw["account_ids"]),
                )
            ],
            "summary": {
                "item_count": len(items),
                "candidate_count": len(candidates),
                "statuses": dict(Counter(item["status"] for item in items)),
                "verified_publications": sum(
                    item["status"] == "confirmed"
                    and item["result"].get("verified") is True
                    and bool(item["external_reference"])
                    for item in items
                ),
                "observed_replies": len(feedback),
            },
            "notes": [
                "这是任务证据快照，不能导入为审批或发送权限。媒体包含文件名及哈希，不包含媒体文件或本机路径。",
                "任务日志及其账号不带任务标记的脱敏读取观测；其他任务的日志不在包内。读取计数是逻辑调用尝试，SDK内部重试及底层网络次数不可用时不计为零。",
                "未知和排期状态保留；已观察回复数量不能代替覆盖完整的回复率。",
            ],
        }

    def review(self, task_id):
        return self.render_review(self.evidence(task_id))

    @staticmethod
    def render_review(evidence):
        def esc(value):
            return html.escape(str(value))

        task = evidence["task"]
        accounts = {a["id"]: a for a in task["accounts"]}
        cards = []
        for item in evidence["items"]:
            payload = item["payload"]
            metadata = {
                key: value for key, value in payload.items() if key not in {"text", "title"}
            }
            account = accounts.get(item["account_id"], {})
            cards.append(
                f"<article><h2>{item['position'] + 1}. {esc(account.get('label', item['account_id']))}</h2>"
                f"<p>{esc(payload.get('action', ''))} · {esc(payload.get('target', ''))} · {esc(item['status'])}</p>"
                f"<h3>{esc(payload.get('title', ''))}</h3><pre>{esc(payload.get('text', ''))}</pre>"
                f"<details><summary>账号、原消息、媒体与精确时间</summary><pre>{esc(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre></details>"
                f"<p>外部回执：{esc(item.get('external_reference') or '未核验')}</p></article>"
            )
        details = {
            "账号与限额": task["accounts"],
            "发现政策与执行窗口": task["policy"],
            "历史审批记录": evidence["approvals"],
            "候选与筛选依据": evidence["candidates"],
        }
        sections = "".join(
            f"<details><summary>{esc(label)}</summary><pre>{esc(json.dumps(value, ensure_ascii=False, indent=2))}</pre></details>"
            for label, value in details.items()
        )
        return (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
            "<title>任务审阅清单</title><style>body{font:16px/1.7 system-ui,sans-serif;color:#24314b;background:#f3f5fa;margin:0}main{max-width:980px;margin:auto;padding:28px}article,details{background:white;padding:18px;margin:16px 0;border:1px solid #dae1ec;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere}h1{font-size:26px}h2{font-size:20px}p{overflow-wrap:anywhere}@media(max-width:600px){main{padding:14px}}</style></head><body><main>"
            f"<h1>{esc(task['name'])}</h1><p>{esc(task['id'])} · {esc(task['mode'])} · {esc(task['state'])} / {esc(task['phase'])}</p>"
            f"<p>导出时间：{esc(evidence['exported_at'])}（北京时间）。此文件是任务快照；在客户端点击开始发送或通过 Codex 明确指令启动。</p>"
            f"<p>清单：{'已冻结' if task['manifest_frozen'] else '草稿，尚未冻结'} · SHA-256：{esc(task['manifest_hash'] or '尚无')}</p>"
            + sections
            + "".join(cards)
            + "<p>已排期不等同于已投递，未知结果不等同于已确认失败。</p></main></body></html>"
        )

    def export(self, task_id, format="zip"):
        if format not in {"zip", "json", "html"}:
            raise Conflict("请选择 zip、json 或 html 导出格式")
        evidence = self.evidence(task_id)
        if format == "json":
            return (json.dumps(evidence, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if format == "html":
            return self.render_review(evidence).encode("utf-8")
        files = {
            key + ".json": (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            for key, value in evidence.items()
            if isinstance(value, (dict, list)) and key != "audit"
        }
        files["metadata.json"] = (
            json.dumps(
                pick(evidence, ("schema_version", "exported_at", "timezone")), ensure_ascii=False
            )
            + "\n"
        ).encode("utf-8")
        files["audit.jsonl"] = "".join(
            canonical(entry) + "\n" for entry in evidence["audit"]
        ).encode("utf-8")
        files["review.html"] = self.render_review(evidence).encode("utf-8")
        csv_buffer = io.StringIO(newline="")
        writer = csv.writer(csv_buffer)
        writer.writerow(
            (
                "sequence",
                "account",
                "action",
                "target",
                "text",
                "scheduled_at",
                "status",
                "verified",
                "external_reference",
            )
        )
        for item in evidence["items"]:
            payload = item["payload"]
            values = (
                item["position"] + 1,
                payload.get("account_label", item["account_id"]),
                payload.get("action", ""),
                payload.get("target", ""),
                payload.get("text", ""),
                payload.get("scheduled_at", ""),
                item["status"],
                item["result"].get("verified", False),
                item["external_reference"] or "",
            )
            writer.writerow(
                "'" + value
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                else value
                for value in values
            )
        files["results.csv"] = csv_buffer.getvalue().encode("utf-8-sig")
        files["checksums.sha256"] = "".join(
            f"{hashlib.sha256(value).hexdigest()}  {name}\n"
            for name, value in sorted(files.items())
        ).encode("ascii")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return output.getvalue()

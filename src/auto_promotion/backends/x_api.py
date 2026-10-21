"""Official X API transport, inside the existing native receipts/authorization boundary.

OAuth user token and documentary approval/consent evidence live in private profiles.
Missing evidence is unknown, never inferred from account health or public keywords.
"""

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..interaction_connectors import source_hash
from ..models import canonical, epoch
from .common import BackendError, read_profile, x_post_weight

FIELDS = "author_id,created_at,conversation_id,entities,referenced_tweets,attachments,lang"
EXPANSIONS = "referenced_tweets.id,referenced_tweets.id.author_id,attachments.media_keys"


class Rejected(BackendError):
    def __init__(self, status):
        super().__init__("XAPIRequestRejected")
        self.status = status


class XHTTP:
    def __init__(self, settings):
        self.settings = settings

    def request(self, method, path, *, params=None, body=None, timeout=30):
        if not self.settings.get("access_token"):
            raise BackendError("XAPITokenMissing")

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        url = "https://api.x.com/2/" + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "Authorization": "Bearer " + self.settings["access_token"],
            "Content-Type": "application/json",
        }
        request = Request(
            url,
            method=method,
            headers=headers,
            data=None if body is None else canonical(body).encode(),
        )
        try:
            with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise BackendError("XAPIResponseTooLarge")
                return json.loads(raw)
        except HTTPError as exc:
            # Only explicit rejections prove a create request was not accepted.
            if exc.code in {400, 401, 403, 404, 422, 429}:
                raise Rejected(exc.code) from None
            raise BackendError("XAPIOutcomeUnknown") from None
        except BackendError:
            raise
        except Exception:
            raise BackendError("XAPITransportOutcomeUnknown") from None


class XAPI:
    def __init__(self, settings, *, http=None, clock=time.time):
        self.settings, self.clock = settings, clock
        self.http = http or XHTTP(settings)

    def _source(self, tweet, includes):
        indexed = {str(t["id"]): t for t in includes.get("tweets", [])}
        refs = tweet.get("referenced_tweets", [])
        context = []
        complete = True
        for ref in refs:
            parent = indexed.get(str(ref["id"]))
            if not parent or not parent.get("text"):
                complete = False
            else:
                context.append(parent["text"])
        media_index = {m["media_key"]: m for m in includes.get("media", [])}
        media = []
        for key in tweet.get("attachments", {}).get("media_keys", []):
            info = media_index.get(key)
            if not info:
                complete = False
            else:
                media.append(
                    {
                        k: info[k]
                        for k in ("media_key", "type", "url", "preview_image_url")
                        if k in info
                    }
                )
        # Media pixels are not inspected by the text classifier; it must leave affected decisions pending.
        if media:
            complete = False
        return {
            "id": str(tweet["id"]),
            "url": "https://x.com/i/web/status/" + str(tweet["id"]),
            "text": tweet["text"],
            "author_id": str(tweet["author_id"]),
            "source_event_at": epoch(tweet["created_at"]),
            "context": "\n".join(context),
            "media": media,
            "language": tweet.get("lang", "unknown"),
            "context_status": "complete" if complete else "missing",
        }

    def lookup(self, event, timeout=30):
        response = self.http.request(
            "GET",
            "tweets/" + event,
            params={
                "tweet.fields": FIELDS,
                "expansions": EXPANSIONS,
                "media.fields": "type,url,preview_image_url",
            },
            timeout=timeout,
        )
        if not response.get("data"):
            raise BackendError("XAPISourceUnavailable")
        return response["data"], response.get("includes", {})

    def discover(self, payload):
        if not payload.get("high_frequency"):
            raise BackendError("XAPIBoundedWindowRequired")
        params = {
            "query": payload["query"],
            "start_time": datetime.fromtimestamp(payload["lower_at"], timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "end_time": datetime.fromtimestamp(payload["upper_at"], timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "max_results": 100,
            "tweet.fields": FIELDS,
            "expansions": EXPANSIONS,
            "media.fields": "type,url,preview_image_url",
        }
        if payload.get("cursor"):
            params["next_token"] = payload["cursor"]
        # Intentionally no since_id: the overlap is a real time window, including older IDs.
        response = self.http.request(
            "GET", "tweets/search/recent", params=params, timeout=payload.get("timeout_seconds", 30)
        )
        if response.get("errors"):
            return {"candidates": [], "coverage_complete": False, "reason": "XAPIPartialSearch"}
        return {
            "candidates": [
                self._source(t, response.get("includes", {})) for t in response.get("data", [])
            ],
            "next_cursor": response.get("meta", {}).get("next_token"),
            "coverage_complete": True,
        }

    def health(self):
        response = self.http.request("GET", "users/me")
        user = response.get("data", {})
        healthy = (
            str(user.get("id")) == str(self.settings.get("user_id"))
            and user.get("username", "").casefold() == self.settings.get("identity", "").casefold()
        )
        return {
            "healthy": healthy,
            "service_ready": healthy,
            "identity_verified": healthy,
            "account_identity": user.get("username", "").casefold() if healthy else None,
            "permissions": {"discover": healthy, "reply": "target_specific_evidence_required"},
            "reason": None if healthy else "XAPIIdentityMismatch",
        }

    def _record(self, value):
        return isinstance(value, dict) and bool(
            value.get("source_reference")
            and value.get("verified_by")
            and value.get("verified_at", 1e15) <= self.clock() < value.get("valid_until", 0)
        )

    def eligibility(self, tweet, includes):
        settings, reasons, evidence = self.settings, [], []
        plan = settings.get("api_plan_evidence", {})
        if not self._record(plan) or plan.get("plan") not in {"self_serve", "enterprise"}:
            reasons.append("channel_capability_unknown")
        elif plan["plan"] == "self_serve":
            mentions = tweet.get("entities", {}).get("mentions", [])
            summoned = any(str(m.get("id")) == str(settings["user_id"]) for m in mentions)
            indexed = {str(t["id"]): t for t in includes.get("tweets", [])}
            summoned |= any(
                ref.get("type") == "quoted"
                and str(indexed.get(str(ref["id"]), {}).get("author_id"))
                == str(settings["user_id"])
                for ref in tweet.get("referenced_tweets", [])
            )
            if not summoned:
                reasons.append("reply_target_ineligible")
            else:
                evidence.append(
                    "author_summoned:" + str(tweet["id"]) + ":" + str(settings["user_id"])
                )
        approval = settings.get("automation_approval", {})
        approved = (
            self._record(approval)
            and str(approval.get("account_user_id")) == str(settings.get("user_id"))
            and approval.get("app_id") == settings.get("app_id")
        )
        try:
            approved = approved and hashlib.sha256(
                Path(approval["document_path"]).read_bytes()
            ).hexdigest() == approval.get("document_sha256")
        except (KeyError, OSError):
            approved = False
        if not approved:
            reasons.append("platform_approval_unverified")
        else:
            evidence.append(approval["source_reference"])
        consent = settings.get("recipient_intents", {}).get(str(tweet["id"]), {})
        if (
            not self._record(consent)
            or str(consent.get("author_id")) != str(tweet["author_id"])
            or str(consent.get("account_user_id")) != str(settings.get("user_id"))
            or consent.get("willing_to_receive") is not True
        ):
            reasons.append("recipient_intent_unverified")
        elif consent.get("opted_out") is not False or self.clock() - consent.get(
            "opt_out_checked_at", 0
        ) > settings.get("consent_check_max_age_seconds", 300):
            reasons.append("recipient_opt_out_unverified")
        else:
            evidence.append(consent["source_reference"])
        return {
            "status": "ineligible"
            if "reply_target_ineligible" in reasons
            else "unknown"
            if reasons
            else "eligible",
            "reasons": reasons,
            "evidence": evidence,
        }

    def preflight(self, payload):
        item = payload["payload"]
        match = re.fullmatch(
            r"https://(?:www\.)?(?:x|twitter)\.com/(?:[\w]+|i/web)/status/(\d+)/?", item["target"]
        )
        if item.get("action") != "reply" or not match:
            return {"allowed": False, "reason": "XAPIReplyTargetRequired"}
        try:
            tweet, includes = self.lookup(match[1], payload.get("timeout_seconds", 30))
        except Rejected as exc:
            return {
                "allowed": False,
                "source_status": "deleted" if exc.status == 404 else "unknown",
                "reason": "XAPISourceUnavailable",
            }
        source = self._source(tweet, includes)
        eligibility = self.eligibility(tweet, includes)
        if item.get("media"):
            eligibility = {
                "status": "ineligible",
                "reasons": ["x_api_media_upload_unavailable"],
                "evidence": [],
            }
        if x_post_weight(item.get("text", "")) > 280:
            eligibility = {
                "status": "ineligible",
                "reasons": ["draft_exceeds_connection_limit"],
                "evidence": [],
            }
        return {
            "allowed": eligibility["status"] == "eligible",
            "eligibility": eligibility,
            "source": source,
            "source_status": "valid",
            "reason": ",".join(eligibility["reasons"]) or None,
        }

    def execute(self, payload, checkpoint, authorize):
        # Bind current context to the strategy's immutable source observation.
        from ..config import database_path
        from ..store import Store

        store = Store(database_path(), initialize=False)
        candidate = store.one(
            "SELECT c.source_hash,p.config FROM hf_candidates c JOIN hf_policies p ON p.id=c.policy_id WHERE c.item_id=?",
            (payload["item_id"],),
        )
        if candidate:
            payload = {
                **payload,
                "timeout_seconds": json.loads(candidate["config"])["publishing"][
                    "request_timeout_seconds"
                ],
            }
        gate = self.preflight(payload)
        if not gate.get("allowed"):
            return {
                "status": "failed_known",
                "submission_started": False,
                "confirmed_not_submitted": True,
                "reason": gate.get("reason", "XAPIEligibilityUnknown"),
            }
        if candidate and candidate["source_hash"] != source_hash(gate["source"]):
            return {
                "status": "failed_known",
                "submission_started": False,
                "confirmed_not_submitted": True,
                "reason": "source_context_changed",
            }
        authorize()
        checkpoint({"submission_started": True})
        try:
            response = self.http.request(
                "POST",
                "tweets",
                body={
                    "text": payload["payload"]["text"],
                    "reply": {"in_reply_to_tweet_id": gate["source"]["id"]},
                },
                timeout=payload.get("timeout_seconds", 30),
            )
        except Rejected as exc:
            return {
                "status": "failed_known",
                "submission_started": False,
                "confirmed_not_submitted": True,
                "reason": "XAPIRequestRejected",
                "rate_limited": exc.status == 429,
                "retry_after_seconds": 900 if exc.status == 429 else 0,
            }
        event_id = str(response.get("data", {}).get("id", ""))
        if not event_id.isdecimal():
            return {"status": "unknown", "reason": "XAPICreateReceiptMissing"}
        result = {
            "status": "unknown",
            "external_reference": "https://x.com/i/web/status/" + event_id,
            "message_ids": [event_id],
            "verified": False,
        }
        checkpoint(result)
        return self.verify({**payload, "external_reference": result["external_reference"]})

    def verify(self, payload):
        reference = payload.get("external_reference") or payload.get("receipt", {}).get(
            "external_reference"
        )
        target = re.search(r"/status/(\d+)", payload["payload"]["target"])[1]
        if reference:
            event_id = re.search(r"/status/(\d+)", reference)
            if not event_id:
                return {"status": "unknown", "verified": False}
            tweet, _ = self.lookup(event_id[1])
            tweets = [tweet]
        else:
            # A crash before saving the ID requires matching this account's own real reply record.
            start = payload.get("attempt_started_at") or payload.get("result", {}).get("started_at")
            params = {"max_results": 100, "tweet.fields": FIELDS}
            if start:
                params["start_time"] = (
                    datetime.fromtimestamp(start, timezone.utc).isoformat().replace("+00:00", "Z")
                )
            response = self.http.request(
                "GET", "users/" + str(self.settings["user_id"]) + "/tweets", params=params
            )
            if response.get("meta", {}).get("next_token") or response.get("errors"):
                return {
                    "status": "unknown",
                    "verified": False,
                    "reason": "verification_coverage_incomplete",
                }
            tweets = response.get("data", [])
        matched = [
            t
            for t in tweets
            if str(t.get("author_id")) == str(self.settings["user_id"])
            and t.get("text") == payload["payload"]["text"]
            and any(
                r.get("type") == "replied_to" and str(r["id"]) == target
                for r in t.get("referenced_tweets", [])
            )
        ]
        if len(matched) != 1:
            return {
                "status": "unknown",
                "verified": False,
                "reason": "reply_not_conclusively_verified",
            }
        tweet = matched[0]
        return {
            "status": "confirmed",
            "verified": True,
            "external_reference": "https://x.com/i/web/status/" + str(tweet["id"]),
            "published_at": epoch(tweet["created_at"]),
            "message_ids": [str(tweet["id"])],
        }


def call(label, operation, payload, checkpoint, authorize):
    api = XAPI(read_profile("x", label))
    if operation == "health":
        return api.health()
    if operation == "discover":
        return api.discover(payload)
    if operation == "preflight":
        return api.preflight(payload)
    if operation == "execute":
        return api.execute(payload, checkpoint, authorize)
    if operation == "verify":
        return api.verify(payload)
    raise BackendError("XAPIOperationUnsupported")

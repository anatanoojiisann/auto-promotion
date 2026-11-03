"""Read metrics from one exact published X post, through its existing connection."""

import json
import re
import time
import uuid
from urllib.parse import urlparse

from .common import BackendError, profile_path, read_profile


def post_id(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
    }:
        raise BackendError("InvalidMetricsReference")
    match = re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}|i/web)/status/(\d+)", parsed.path.rstrip("/"))
    if not match:
        raise BackendError("InvalidMetricsReference")
    return match[1]


def parse_count(value):
    """Never treat missing/hidden counters as zero; mark compact displays approximate."""
    if not isinstance(value, str):
        return None, False
    value = value.replace("\u202f", "").replace("\u00a0", "")
    match = re.search(
        r"(?<![\w.\-,])([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*([KMB万亿千]?)(?![0-9.,])", value, re.I
    )
    if not match:
        return None, False
    amount, suffix = match.groups()
    multiplier = {
        "": 1,
        "K": 1000,
        "M": 1000000,
        "B": 1000000000,
        "万": 10000,
        "亿": 100000000,
        "千": 1000,
    }[suffix.upper()]
    if "." in amount and not suffix:
        return None, False  # Ambiguous localized separator.
    count = round(float(amount.replace(",", "")) * multiplier)
    return (count, bool(suffix)) if 0 <= count < 2**63 else (None, False)


EXTRACT_JS = r"""(root, expected) => {
  const time = root.querySelector('time');
  const href = time && time.closest('a')?.getAttribute('href');
  const match = href && href.match(/^\/([A-Za-z0-9_]{1,15})\/status\/(\d+)(?:\?.*)?$/);
  if (!match || match[2] !== expected.id || match[1].toLowerCase() !== expected.identity) return {subject:false};
  const label = selector => {
    const nodes = [...root.querySelectorAll(selector)].filter(n => n.closest('[data-testid="tweet"]') === root);
    if (nodes.length !== 1) return null;
    const n = nodes[0];
    return [n.getAttribute('aria-label'), n.textContent].filter(Boolean);
  };
  return {subject:true, views:label('a[href$="/analytics"],a[href*="/analytics?"]'),
    likes:label('[data-testid="like"],[data-testid="unlike"]'),
    replies:label('[data-testid="reply"]'),
    reposts:label('[data-testid="retweet"],[data-testid="unretweet"]')};
}"""


def metrics_result(observed, reference, *, source="x_visible_dom"):
    if not isinstance(observed, dict) or observed.get("subject") is not True:
        return {"verified": False, "reason": "MetricsSubjectUnverified"}
    metrics, estimated = {}, []
    for field in ("views", "likes", "replies", "reposts"):
        candidates = observed.get(field)
        parsed = [parse_count(v) for v in candidates] if isinstance(candidates, list) else []
        parsed = [v for v in parsed if v[0] is not None]
        # Accessibility labels often contain the full count, even when visible text is compact.
        exact = [v for v in parsed if not v[1]]
        value, approximate = (exact or parsed or [(None, False)])[0]
        metrics[field] = value
        if approximate:
            estimated.append(field)
    return {
        "verified": True,
        "engagement_subject_verified": True,
        "external_reference": reference,
        "engagement_metrics": metrics,
        "engagement_estimated": estimated,
        "engagement_observed_at": time.time(),
        "engagement_source": source,
    }


def api_metrics(settings, reference, identifier):
    from .x_api import XAPI, Rejected

    api = XAPI(settings)
    try:
        if not api.health()["healthy"]:
            raise BackendError("XAPIIdentityMismatch")
        response = api.http.request(
            "GET", "tweets/" + identifier, params={"tweet.fields": "author_id,public_metrics"}
        )
        data = response.get("data", {})
        if str(data.get("id")) != identifier or str(data.get("author_id")) != str(
            settings.get("user_id")
        ):
            raise BackendError("MetricsSubjectUnverified")
        public = data.get("public_metrics", {})
        return {
            "verified": True,
            "engagement_subject_verified": True,
            "external_reference": reference,
            "engagement_observed_at": time.time(),
            "engagement_source": "x_api_public_metrics",
            "engagement_metrics": {
                "views": public.get("impression_count"),
                "likes": public.get("like_count"),
                "replies": public.get("reply_count"),
                "reposts": public.get("retweet_count"),
            },
        }
    except Rejected as exc:
        return {
            "verified": False,
            "reason": "XAPIReadRejected",
            "rate_limited": exc.status == 429,
            "retry_after_seconds": exc.retry_after_seconds,
            "rate_limit_reset_at": exc.reset_at,
        }


def tabbit_metrics_program(expected, reference, identifier):
    from .browser_status import STATUS_JS
    from .tabbit import IDENTITY_JS, READ_STATE_JS

    return (
        "const expected="
        + json.dumps(expected)
        + ";\nconst readStatus=()=>page.evaluate("
        + STATUS_JS
        + ");\n"
        + READ_STATE_JS
        + IDENTITY_JS
        + "\n"
        + r"""
try {
  await page.setViewportSize({width:1440,height:1000});
  let blocked = await readOnlyState(); if (blocked) return blocked;
  const response = await page.goto(REFERENCE, {waitUntil:'domcontentloaded',timeout:25000});
  blocked = await readOnlyState(); if (blocked) return blocked;
  if (response && response.status()===429) return {rate_limited:true,retry_after_seconds:900,reason:'XReadRateLimited'};
  if (response && response.status()>=400) return {reason:'XMetricsPageUnavailable'};
  const status = await readStatus(); if (status) return {reason:status};
  const menu = page.getByTestId('SideNav_AccountSwitcher_Button');
  await menu.waitFor({state:'visible',timeout:15000});
  // X omits the handle entirely in its compact sidebar. Wait for the full
  // account menu to hydrate, then retain both existing identity checks.
  await expect(menu).toContainText(/@[A-Za-z0-9_]{1,15}\b/, {timeout:15000});
  await checkIdentity();
  const article = await targetArticle(REFERENCE);
  const observed = await article.evaluate(EXTRACT, {id:IDENTIFIER,identity:expected});
  blocked = await readOnlyState(); if (blocked) return blocked;
  return observed;
} finally { await page.close(); }
""".replace("REFERENCE", json.dumps(reference))
        .replace("IDENTIFIER", json.dumps(identifier))
        .replace("EXTRACT", EXTRACT_JS)
    )


def tabbit_metrics(settings, reference, identifier):
    from .tabbit import cli

    instance, expected = settings.get("tabbit_instance", ""), settings.get("identity", "")
    if not re.fullmatch(r"[0-9A-F]{16}", instance) or not re.fullmatch(
        r"[a-z0-9_]{1,15}", expected
    ):
        raise BackendError("InvalidTabbitConfiguration")
    task = "Read X metrics " + expected
    inventory = cli(["create", "--task", task, "--limit", "200"], instance)
    if inventory.get("task", {}).get("reused") or inventory.get("truncated"):
        raise BackendError("TabbitMetricsPending")
    request_id, settled = "metrics-" + uuid.uuid4().hex, False
    code = tabbit_metrics_program(expected, reference, identifier)
    try:
        response = cli(
            ["nodejs", "--task", task, "--request-id", request_id, "--timeout-ms", "60000"],
            instance,
            code=code,
        )
        if response.get("status") in {"queued", "running"}:
            response = cli(
                ["receipt", "--task", task, "--request-id", request_id, "--wait-ms", "60000"],
                instance,
            )
        settled = response.get("status") in {"succeeded", "failed", "interrupted"}
        if not settled:
            raise BackendError("TabbitMetricsPending")
        if response.get("status") != "succeeded":
            failure = response.get("result", {})
            if failure.get("error") == "XIdentityMismatch":
                raise BackendError("BrowserIdentityMismatch")
            raise BackendError("XMetricsReadUnavailable")
        observed = response.get("result", {}).get("value", {})
        return metrics_result(observed, reference) if observed.get("subject") else observed
    finally:
        if settled:
            cli(["finish", "--task", task], instance)


def call_x(label, payload, checkpoint):
    try:
        return _call_x(label, payload)
    except BackendError as exc:
        result = {"verified": False, "reason": exc.safe_code}
        if exc.safe_code in {"BrowserRateLimited", "XReadRateLimited"}:
            result.update(rate_limited=True, retry_after_seconds=900)
        if getattr(exc, "account_block_reason", None):
            result["account_block_reason"] = exc.account_block_reason
            checkpoint(result)
        return result


def _call_x(label, payload):
    if payload.get("read_only") is not True:
        raise BackendError("MetricsReadOnlyRequired")
    reference = payload.get("external_reference", "")
    identifier, settings = post_id(reference), read_profile("x", label)
    if settings.get("connection_type") == "x_api":
        return api_metrics(settings, reference, identifier)
    if settings.get("browser_channel") == "tabbit":
        return tabbit_metrics(settings, reference, identifier)
    if settings.get("browser_channel") == "browser_use":
        raise BackendError("MetricsConnectionUnsupported")
    from playwright.sync_api import sync_playwright

    from .browser import BrowserBackend, launch_context
    from .browser_status import check_context_status

    with sync_playwright() as runtime:
        context = launch_context(
            runtime,
            profile_path("x", label),
            browser_channel=settings.get("browser_channel", "chromium"),
            headless=settings.get("browser_headless", True) is not False,
        )
        try:
            check_context_status(context)
            page = context.new_page()
            page.set_default_timeout(15000)
            backend = BrowserBackend("x", label, context, page, settings)
            backend.navigate_x(reference)
            backend.check_identity()
            article = page.get_by_test_id("tweet").filter(
                has=page.locator(f'a[href$="/status/{identifier}"] time')
            )
            article.wait_for(state="visible", timeout=15000)
            if article.count() != 1:
                raise BackendError("MetricsSubjectAmbiguous")
            observed = article.evaluate(
                EXTRACT_JS, {"id": identifier, "identity": settings["identity"]}
            )
            backend.check_state()
            return metrics_result(observed, reference)
        finally:
            context.close()

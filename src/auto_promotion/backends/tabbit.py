"""X session checks and account-opted-in replies through Tabbit's stable launcher."""

import json
import math
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse

from ..models import digest, epoch
from .browser import read_limits
from .browser_status import STATUS_JS
from .common import BackendError, language_evidence, observed_timestamp, owned_x_target
from .x_text import EDITOR_TEXT_JS, TWEET_TEXT_JS


def read_counter(value):
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def resource_counters(value):
    if not isinstance(value, dict) or any(
        key not in {"image", "media", "font", "script", "stylesheet", "third_party"}
        or read_counter(count) is None
        for key, count in value.items()
    ):
        return None
    return dict(value)


def launcher_path():
    if os.name == "nt":
        return Path.home() / "AppData/Local/Tabbit/LocalAgent/bin/tabbit-cli.exe"
    return Path.home() / ".local/bin/tabbit-cli"


def cli(arguments, instance, *, code=None, timeout_seconds=75):
    launcher = launcher_path()
    if not launcher.is_file():
        raise BackendError("TabbitLauncherUnavailable")
    env = os.environ.copy()
    env["TABBIT_PLAYWRIGHT_INSTANCE"] = instance
    try:
        # File-backed UTF-8 stdin follows the documented Windows input recipe
        # without passing JavaScript through a shell's quoting rules.
        with ExitStack() as stack:
            source = None
            if code is not None:
                source = stack.enter_context(tempfile.TemporaryFile())
                source.write(code.encode("utf-8"))
                source.seek(0)
            result = subprocess.run(
                [str(launcher), *arguments],
                stdin=source,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=timeout_seconds,
                check=False,
            )
        value = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise BackendError("TabbitConnectionUnavailable") from None
    if result.returncode not in {0, 70, 75} or value.get("ok") is False:
        raise BackendError("TabbitConnectionUnavailable")
    return value


def call(platform, settings, operation, payload=None, checkpoint=None, authorize=None):
    if platform != "x":
        raise BackendError("UnsupportedPlatformOperation")
    if operation == "discover" and payload and payload.get("high_frequency"):
        if payload.get("read_only") is not True:
            raise BackendError("TabbitHighFrequencyReadOnlyRequired")
        return hf_discover(settings, payload, checkpoint=checkpoint)
    if operation in {"execute", "verify", "preflight"} and "reply" in settings.get(
        "automatic_actions", []
    ):
        if operation == "preflight":
            return {"allowed": True}
        return reply(settings, operation, payload, checkpoint, authorize)
    # This connection verifies the existing login only. It never exports cookies
    # or executes interactions, even if another caller enables live writes.
    if operation == "preflight":
        return {"allowed": False, "reason": "TabbitReadOnlyConnection"}
    if operation != "health":
        raise BackendError("TabbitReadOnlyConnection")
    expected = settings.get("identity", "")
    instance = settings.get("tabbit_instance", "")
    if not re.fullmatch(r"[a-z0-9_]{1,15}", expected) or not re.fullmatch(
        r"[0-9A-F]{16}", instance
    ):
        raise BackendError("InvalidTabbitConfiguration")

    task = "Verify X " + expected
    inventory = cli(["create", "--task", task, "--limit", "200"], instance)
    if inventory.get("task", {}).get("reused"):
        # Preserve a previous unsettled request instead of bypassing its receipt.
        raise BackendError("TabbitCheckPending")
    settled = True
    health_result = None
    try:
        if inventory.get("truncated"):
            raise BackendError("TabbitInventoryTruncated")
        request_id = "identity-" + uuid.uuid4().hex
        arguments = ["nodejs", "--task", task, "--request-id", request_id, "--timeout-ms", "60000"]
        # A settled sender tab can still fail attachment before any health code
        # runs. A new task without --tab gets its own scratch page in the same
        # configured browser session; user tabs and their drafts stay untouched.
        # Account menu and Profile are observed controls on the authenticated X UI.
        code = (
            READ_STATE_JS
            + "const readStatus = () => page.evaluate("
            + STATUS_JS
            + ");\n"
            + r"""
try {
let blocked = await readOnlyState();
if (blocked) return {...blocked,read_contract:'repo_x_scan_v1',read_observed_at:Date.now()/1000};
if (!/^https:\/\/(www\.)?x\.com\//.test(page.url())) {
  await page.goto("https://x.com/home", {waitUntil: "domcontentloaded"});
}
blocked = await readOnlyState();
if (blocked) return {...blocked,read_contract:'repo_x_scan_v1',read_observed_at:Date.now()/1000};
let blocked_reason = await readStatus();
if (blocked_reason) return {blocked_reason};
const menu = page.getByTestId("SideNav_AccountSwitcher_Button");
try {await menu.waitFor({state: "attached", timeout: 45000});} catch (error) {
  blocked = await readOnlyState();
  if (blocked) return {...blocked,read_contract:'repo_x_scan_v1',read_observed_at:Date.now()/1000};
  blocked_reason = await readStatus();
  if (blocked_reason) return {blocked_reason};
  throw error;
}
blocked_reason = await readStatus();
if (blocked_reason) return {blocked_reason};
const text = await menu.textContent();
const handles = [...new Set([...text.matchAll(/@([A-Za-z0-9_]{1,15})\b/g)]
  .map(m => m[1].toLowerCase()))];
const profile = await page.getByRole("link", {name: "Profile", exact: true, includeHidden: true})
  .getAttribute("href");
blocked = await readOnlyState();
return {...(blocked || {handles,profile}),read_contract:'repo_x_scan_v1',read_observed_at:Date.now()/1000};
} finally {
  await page.close();
}
"""
        )
        settled = False
        response = cli(arguments, instance, code=code)
        status = response.get("status")
        if status in {"queued", "running"}:
            response = cli(
                ["receipt", "--task", task, "--request-id", request_id, "--wait-ms", "60000"],
                instance,
            )
            status = response.get("status")
        settled = status in {"succeeded", "failed", "interrupted"}
        if not settled:
            raise BackendError("TabbitCheckPending")
        if status != "succeeded":
            raise BackendError("TabbitIdentityUnavailable")
        observed = response.get("result", {}).get("value", {})
        if observed.get("blocked_reason") in {
            "BrowserVerificationRequired",
            "BrowserAccountRestricted",
        }:
            return {
                "healthy": False,
                "service_ready": True,
                "identity_verified": False,
                "permissions": [],
                "reason": observed["blocked_reason"],
            }
        handles = observed.get("handles", [])
        verified = (
            handles == [expected] and str(observed.get("profile", "")).casefold() == "/" + expected
        )
        health_result = {
            "healthy": verified,
            "service_ready": True,
            "identity_verified": verified,
            "account_identity": expected if verified else None,
            "permissions": [],
            "reason": "tabbit_session_identity_verified" if verified else "TabbitIdentityMismatch",
            "read_observed_at": observed.get("read_observed_at")
            if observed.get("read_contract") == "repo_x_scan_v1"
            else None,
            "read_observation_source": "tabbit_identity_dom",
            "live_probe": False,
        }
        blocked = observed.get("account_block_reason")
        if observed.get("read_contract") == "repo_x_scan_v1" and blocked in {
            "x_challenge",
            "x_login_required",
            "x_account_restricted",
        }:
            health_result.update(
                healthy=False,
                identity_verified=False,
                account_identity=None,
                account_block_reason=blocked,
                account_block_event_id=uuid.uuid4().hex,
                reason={
                    "x_challenge": "XChallengeRequired",
                    "x_login_required": "XLoginRequired",
                    "x_account_restricted": "XAccountRestricted",
                }[blocked],
            )
            if checkpoint:
                checkpoint(health_result)
        return health_result
    finally:
        if settled:
            try:
                finished = cli(["finish", "--task", task], instance)
                if finished.get("forcedCleanup") or finished.get("ok") is False:
                    raise BackendError("TabbitFinishIncomplete")
            except BackendError:
                if health_result and health_result.get("account_block_reason"):
                    health_result["read_cleanup_state"] = "incomplete"
                else:
                    raise


IDENTITY_JS = (
    "const editorText = async locator => await locator.evaluate("
    + EDITOR_TEXT_JS
    + ");\n"
    + "const tweetText = async locator => await locator.evaluate("
    + TWEET_TEXT_JS
    + ");\n"
    + r"""
async function checkIdentity() {
  const menu = page.getByTestId("SideNav_AccountSwitcher_Button");
  await menu.waitFor({state: "attached", timeout: 45000});
  const handles = [...new Set([...(await menu.textContent()).matchAll(/@([A-Za-z0-9_]{1,15})\b/g)]
    .map(m => m[1].toLowerCase()))];
  const profile = await page.getByRole("link", {name: "Profile", exact: true, includeHidden: true}).getAttribute("href");
  assert(handles.length === 1 && handles[0] === expected && profile.toLowerCase() === "/" + expected,
    "XIdentityMismatch");
}
async function targetArticle(url) {
  const id = new URL(url).pathname.split("/").pop();
  const article = page.getByTestId("tweet").filter({has: page.locator(`a[href$="/status/${id}"] time`)});
  await expect(article).toHaveCount(1, {timeout: 15000});
  return article;
}
async function composerControls() {
  const dialogs = page.locator('[role="dialog"]:visible').filter({has:page.getByTestId("tweetTextarea_0")});
  const count = await dialogs.count();
  assert(count <= 1, "XReplyComposerAmbiguous");
  const scope = count === 1 ? dialogs : page;
  const editor = scope.getByTestId("tweetTextarea_0");
  await expect(editor).toHaveCount(1);
  return {editor, scope, button: scope.getByTestId(count === 1 ? "tweetButton" : "tweetButtonInline")};
}
"""
)


READ_STATE_JS = r"""
async function readOnlyState() {
  const url = new URL(page.url());
  if (url.protocol !== 'https:' || !['x.com','www.x.com','twitter.com','www.twitter.com'].includes(url.hostname)) return null;
  const path = url.pathname.replace(/\/$/,'').toLowerCase();
  if (['/account/access','/i/flow/arkose','/i/flow/challenge'].includes(path))
    return {account_block_reason:'x_challenge',read_state:'challenge',reason:'XChallengeRequired'};
  if (['/login','/i/flow/login'].includes(path))
    return {account_block_reason:'x_login_required',read_state:'login_required',reason:'XLoginRequired'};
  if (['/account/suspended','/account/locked'].includes(path))
    return {account_block_reason:'x_account_restricted',read_state:'restricted',reason:'XAccountRestricted'};
  for (const selector of ['iframe[src^="https://client-api.arkoselabs.com/"]',
    'iframe[src^="https://iframe.arkoselabs.com/"]','iframe[src^="https://challenges.cloudflare.com/"]',
    '#arkoseFrame','[data-testid="arkoseChallenge"]','[data-testid="accountSuspended"]','[data-testid="accountLocked"]']) {
    const matches = page.locator(selector);
    for (let i=0; i<await matches.count(); i++) if (await matches.nth(i).isVisible()) {
      const restricted = selector.includes('account');
      return {account_block_reason:restricted?'x_account_restricted':'x_challenge',
        read_state:restricted?'restricted':'challenge',reason:restricted?'XAccountRestricted':'XChallengeRequired'};
    }
  }
  return null;
}
"""

READ_ONLY_SCAN_JS = (
    READ_STATE_JS
    + r"""
const deadline = Date.now() + scanTimeoutMs;
const scan = {read_contract:'repo_x_scan_v1',read_primary_request_count:0,
  read_primary_response_count:0,read_primary_requests_blocked:0,read_resource_failures:{}};
const resourceSeen = new Set();
function primary(request) {
  const url = new URL(request.url());
  if (url.protocol !== 'https:' || !['x.com','www.x.com','api.x.com','twitter.com','www.twitter.com','api.twitter.com'].includes(url.hostname)) return false;
  if (request.isNavigationRequest()) return request.frame()===page.mainFrame() && ['/','/home','/search'].includes(url.pathname);
  if (!['xhr','fetch'].includes(request.resourceType())) return false;
  const parts = url.pathname.replace(/\/$/,'').split('/');
  return (parts.includes('graphql') && ['SearchTimeline','SearchTimelineV2'].includes(parts.at(-1))) ||
    ['/i/api/2/search/adaptive.json','/2/search/adaptive.json','/1.1/search/tweets.json','/i/api/1.1/search/tweets.json'].includes(url.pathname);
}
function resourceFailure(request) {
  if (resourceSeen.has(request)) return;
  resourceSeen.add(request);
  const resource = request.resourceType();
  const kind = ['image','media','font','script','stylesheet'].includes(resource) ? resource : 'third_party';
  scan.read_resource_failures[kind] = (scan.read_resource_failures[kind] || 0) + 1;
}
function stop() {
  if (scan.read_state) throw new Error(scan.reason);
  if (Date.now() >= deadline) {
    scan.read_state='timeout';scan.reason='BrowserReadDeadlineExceeded';throw new Error(scan.reason);
  }
}
function remaining(cap=15000) {stop();return Math.max(1,Math.min(cap,deadline-Date.now()));}
const onRoute = async route => {
  if (primary(route.request())) {
    if (scan.read_state || (requestLimit !== null && scan.read_primary_request_count >= requestLimit)) {
      scan.read_primary_requests_blocked++;
      if (!scan.read_state) {scan.read_state='budget_exhausted';scan.reason='BrowserReadBudgetExhausted';}
      await route.abort('aborted');return;
    }
    scan.read_primary_request_count++;
  }
  await route.fallback();
};
const onResponse = response => {
  if (primary(response.request())) {
    scan.read_primary_response_count++;
    if (response.status() === 429) {
      const value = response.headers()['retry-after'] || '';
      const parsed = /^\d+$/.test(value) ? Number(value) : (Date.parse(value)-Date.now())/1000;
      scan.retry_after_seconds = Number.isFinite(parsed) && parsed>0 ? Math.ceil(parsed) : 900;
      scan.rate_limited=true;scan.read_primary_rate_limited=true;
      scan.read_state='rate_limited';scan.reason='BrowserRateLimited';scan.read_observed_at=Date.now()/1000;
    }
  } else if (response.status() >= 400) resourceFailure(response.request());
};
const onFailed = request => {
  if (primary(request)) {
    if (!scan.read_state) {scan.read_state='network_error';scan.reason='BrowserSearchRequestFailed';}
  } else resourceFailure(request);
};
async function check() {const state=await readOnlyState();if(state) {Object.assign(scan,state);throw new Error(state.reason);}stop();}
async function wait(locator) {
  while (true) {
    await check();
    try {await locator.waitFor({state:'visible',timeout:remaining(250)});await check();return;}
    catch(error) {if (error.name !== 'TimeoutError') throw error;}
  }
}
async function identity() {
  const menu = page.getByTestId('SideNav_AccountSwitcher_Button');
  await wait(menu);
  const handles = [...new Set([...(await menu.textContent()).matchAll(/@([A-Za-z0-9_]{1,15})\b/g)].map(m=>m[1].toLowerCase()))];
  const profile = await page.getByRole('link',{name:'Profile',exact:true,includeHidden:true}).getAttribute('href');
  assert(handles.length===1 && handles[0]===expected && profile.toLowerCase()==='/'+expected,'XIdentityMismatch');
  await check();
}
await page.route('**/*',onRoute);
page.on('response',onResponse);page.on('requestfailed',onFailed);
try {
  await check();
  await page.goto('https://x.com/home',{waitUntil:'domcontentloaded',timeout:remaining(30000)});
  await identity();
  await page.goto('https://x.com/search?q='+encodeURIComponent(query)+'&src=typed_query&f=live',
    {waitUntil:'domcontentloaded',timeout:remaining(30000)});
  await check();
  const observedUrl = new URL(page.url());
  assert(observedUrl.protocol==='https:' && ['x.com','www.x.com'].includes(observedUrl.hostname) && observedUrl.pathname==='/search','TabbitSearchUnavailable');
  const articles = page.locator('[data-testid="primaryColumn"] [data-testid="tweet"]');
  await wait(articles.first());
  const posts = await page.evaluate(() => [...document.querySelectorAll(
    '[data-testid="primaryColumn"] [data-testid="tweet"]')].slice(0,100).flatMap(article => {
      const links=[...article.querySelectorAll('a[href*="/status/"]')].filter(a=>a.querySelector('time'));
      const texts=article.querySelectorAll('[data-testid="tweetText"]');
      if (links.length!==1 || texts.length!==1) return [];
      const match=new URL(links[0].href).pathname.match(/^\/([A-Za-z0-9_]+)\/status\/(\d+)$/);
      const datetime=links[0].querySelector('time')?.getAttribute('datetime');
      const text=texts[0].innerText || '';
      return match && datetime && text.trim() ? [{id:match[2],author_id:'@'+match[1],datetime,text,
        query_visible_text:(article.innerText || '').slice(0,2000)}] : [];
  }));
  await check();
  return {...scan,url:page.url(),posts,read_state:'partial',read_page_complete:true,
    read_observed_at:Date.now()/1000};
} catch(error) {
  const state=await readOnlyState().catch(()=>null);
  if (state) Object.assign(scan,state);
  return {...scan,url:page.url(),posts:[],read_state:scan.read_state ||
    (error.name==='TimeoutError'?'timeout':'unobservable'),read_page_complete:false,
    reason:scan.reason || (/^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(error.message)?error.message:'TabbitReadOnlySearchUnavailable')};
} finally {
  await page.unroute('**/*',onRoute);
  page.removeListener('response',onResponse);page.removeListener('requestfailed',onFailed);
  if (scratch) {try {await page.close();} catch (_) {}}
}
"""
)


def hf_discover(settings, payload, *, checkpoint=None):
    """Observe public X search results in the authenticated Tabbit tab only."""
    if payload.get("cursor"):
        raise BackendError("TabbitSearchCursorUnavailable")
    seconds, request_limit = read_limits(payload)
    deadline = time.monotonic() + seconds

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise BackendError("TabbitReadOnlyDeadlineExceeded")
        return value

    def bounded_cli(arguments, instance, **kwargs):
        return cli(arguments, instance, timeout_seconds=max(0.001, min(75, remaining())), **kwargs)

    expected, instance = settings.get("identity", ""), settings.get("tabbit_instance", "")
    if not re.fullmatch(r"[a-z0-9_]{1,15}", expected) or not re.fullmatch(
        r"[0-9A-F]{16}", instance
    ):
        raise BackendError("InvalidTabbitConfiguration")
    task = "X read-only scan " + expected
    inventory = bounded_cli(["create", "--task", task, "--limit", "200"], instance)
    if inventory.get("task", {}).get("reused") or inventory.get("truncated"):
        raise BackendError("TabbitInventoryUnavailable")
    available = [
        tab
        for tab in inventory.get("tabs", [])
        if tab.get("state") == "available"
        and urlparse(tab.get("url", "")).hostname in {"x.com", "www.x.com"}
    ]
    preferred = [tab for tab in available if tab.get("tabId") == settings.get("tabbit_tab_id")]
    if not preferred:
        preferred = [
            tab
            for tab in available
            if urlparse(tab["url"]).path.strip("/").split("/")[0].casefold() == expected
        ]
    selected = preferred[0] if len(preferred) == 1 else None
    if selected is None and len(available) == 1:
        selected = available[0]

    code = (
        "const expected = "
        + json.dumps(expected)
        + "; const query = "
        + json.dumps(payload["query"], ensure_ascii=False)
        + "; const scratch = "
        + json.dumps(selected is None)
        + ";\n"
        + "const scanTimeoutMs = "
        + json.dumps(max(1, int(remaining() * 1000)))
        + "; const requestLimit = "
        + json.dumps(request_limit)
        + ";\n"
        + READ_ONLY_SCAN_JS
    )
    settled = False
    scan_result = None
    try:
        request_id = "observe-" + uuid.uuid4().hex
        arguments = ["nodejs", "--task", task]
        if selected:
            arguments.extend(["--tab", str(selected["tabId"])])
        arguments.extend(
            ["--request-id", request_id, "--timeout-ms", str(max(1, int(remaining() * 1000)))]
        )
        response = bounded_cli(arguments, instance, code=code)
        if response.get("status") in {"queued", "running"}:
            response = bounded_cli(
                [
                    "receipt",
                    "--task",
                    task,
                    "--request-id",
                    request_id,
                    "--wait-ms",
                    str(max(1, int(remaining() * 1000))),
                ],
                instance,
            )
        settled = response.get("status") in {"succeeded", "failed", "interrupted"}
        if not settled:
            raise BackendError("TabbitReadOnlyPending")
        if response.get("status") != "succeeded":
            raise BackendError("TabbitReadOnlySearchUnavailable")
        result = response.get("result", {}).get("value", {})
        parsed = urlparse(result.get("url", ""))
        trusted = result.get("read_contract") == "repo_x_scan_v1"
        blocked = result.get("account_block_reason") if trusted else None
        if blocked not in {"x_challenge", "x_login_required", "x_account_restricted"}:
            blocked = None
        stopped = trusted and result.get("read_state") in {
            "rate_limited",
            "budget_exhausted",
            "timeout",
            "network_error",
            "unobservable",
        }
        if (
            not blocked
            and not stopped
            and (
                parsed.scheme != "https"
                or parsed.hostname not in {"x.com", "www.x.com"}
                or parsed.path != "/search"
            )
        ):
            raise BackendError("TabbitSearchUnavailable")
        observed = []
        for post in result.get("posts", [])[:100]:
            if not isinstance(post, dict) or not re.fullmatch(r"\d+", str(post.get("id", ""))):
                continue
            try:
                at = epoch(post["datetime"])
            except (KeyError, TypeError, ValueError):
                continue
            if not payload["lower_at"] <= at <= payload["upper_at"]:
                continue
            content = str(post.get("text", ""))
            if not content.strip():
                continue
            language, _ = language_evidence(content)
            observed.append(
                {
                    "id": str(post["id"]),
                    "url": "https://x.com/i/web/status/" + str(post["id"]),
                    "text": content,
                    "author_id": post.get("author_id", ""),
                    "source_event_at": at,
                    "context_status": "missing",
                    "context": "",
                    "language": language,
                    "query_visible_text": str(post.get("query_visible_text", ""))[:2000],
                }
            )
        primary_limited = (
            trusted
            and result.get("rate_limited") is True
            and result.get("read_primary_rate_limited") is True
        )
        if primary_limited or blocked or stopped:
            observed = []
        observed_at = result.get("read_observed_at") if trusted else None
        if (
            type(observed_at) not in {int, float}
            or not math.isfinite(observed_at)
            or observed_at < 0
        ):
            observed_at = None
        state = result.get("read_state") if trusted else None
        if state not in {
            "partial",
            "unobservable",
            "rate_limited",
            "budget_exhausted",
            "timeout",
            "network_error",
            "challenge",
            "login_required",
            "restricted",
        }:
            state = None
        scan_result = {
            "candidates": observed,
            "read_page_complete": bool(
                not (primary_limited or blocked or stopped)
                and (result.get("read_page_complete") is True or observed)
            ),
            "read_state": state or ("partial" if observed else "unobservable"),
            "read_observed_at": observed_at,
            "read_observation_source": "tabbit_repository_primary_response"
            if primary_limited
            else "tabbit_repository_dom"
            if observed_at is not None
            else "tabbit_unstamped_receipt",
            "read_page_fingerprint": digest(sorted(row["id"] for row in observed))
            if observed
            else None,
            "read_resource_failures": resource_counters(result.get("read_resource_failures"))
            if trusted
            else None,
            "read_primary_request_count": read_counter(result.get("read_primary_request_count"))
            if trusted
            else None,
            "read_primary_response_count": read_counter(result.get("read_primary_response_count"))
            if trusted
            else None,
            "read_primary_requests_blocked": read_counter(
                result.get("read_primary_requests_blocked")
            )
            if trusted
            else None,
            "read_request_count_source": "tabbit_repository_navigation_and_search_attempts"
            if trusted
            else "unavailable",
            "next_cursor": None,
            "coverage_complete": False,
            "reason": "browser_window_not_exhaustive"
            if result.get("posts")
            else "browser_results_not_observable",
        }
        if primary_limited:
            delay = result.get("retry_after_seconds", 900)
            scan_result.update(
                rate_limited=True,
                retry_after_seconds=delay if type(delay) is int and delay > 0 else 900,
                reason="BrowserRateLimited",
            )
        if blocked:
            scan_result.update(
                account_block_reason=blocked,
                account_block_event_id=uuid.uuid4().hex,
                reason={
                    "x_challenge": "XChallengeRequired",
                    "x_login_required": "XLoginRequired",
                    "x_account_restricted": "XAccountRestricted",
                }[blocked],
            )
            if checkpoint:
                checkpoint(scan_result)
        return scan_result
    finally:
        if settled:
            try:
                finished = cli(["finish", "--task", task], instance, timeout_seconds=5)
                if finished.get("forcedCleanup") or finished.get("ok") is False:
                    raise BackendError("TabbitFinishIncomplete")
            except BackendError:
                if scan_result and (
                    scan_result.get("rate_limited") or scan_result.get("account_block_reason")
                ):
                    scan_result["read_cleanup_state"] = "incomplete"
                else:
                    raise


PREPARE_JS = r"""
await page.goto(item.action === "post" ? "https://x.com/compose/post" : item.target, {waitUntil: "domcontentloaded", timeout:45000});
await checkIdentity();
if (item.action === "post") await page.locator('[role="dialog"]:visible')
  .filter({has:page.getByTestId("tweetTextarea_0")}).waitFor({state:"visible",timeout:45000});
if (item.action === "reply") {
const article = await targetArticle(item.target);
const reply = article.getByTestId("reply").first();
await expect(reply).toBeVisible();
await expect(reply).toBeEnabled();
await reply.click();
// The background editor stays visible while the reply modal is mounting.
// Wait for the modal itself; choosing a visible editor immediately races it.
await page.locator('[role="dialog"]:visible').filter({has:page.getByTestId("tweetTextarea_0")})
  .waitFor({state:"visible", timeout:15000});
}
const {editor, scope, button} = await composerControls();
await editor.waitFor({state: "visible", timeout: 45000});
if (item.action === "post") {
  const draft = await editorText(editor);
  assert((!draft.trim() || draft === item.text) &&
    await scope.locator('[data-testid="attachments"] img, [data-testid="attachments"] video').count() === 0, "XComposerNotEmpty");
  if (draft !== item.text) {
    await editor.click(); await editor.press("Control+A"); await editor.press("Backspace");
    const lines = item.text.split(String.fromCharCode(10));
    for (let i=0;i<lines.length;i++) {
      if (i) await editor.press("Enter");
      if (lines[i]) await editor.pressSequentially(lines[i]);
    }
  }
  assert(await editorText(editor) === item.text, "ApprovedTextNotRetained");
} else {
  await editor.click();
  await editor.press("Control+A");
  await editor.press("Backspace");
  await editor.pressSequentially(item.text);
  await expect(editor).toHaveText(item.text);
}
if (item.media.length) await scope.locator('input[type="file"]').setInputFiles(item.media.map(m => m.path));
if (item.action === "post") {
  await expect(scope.locator('[data-testid="attachments"] img')).toHaveCount(item.media.length, {timeout:45000});
  await expect(scope.locator('[data-testid="attachments"] [role="progressbar"]')).toHaveCount(0, {timeout:45000});
}
await expect(button).toBeEnabled({timeout: 45000});
return {prepared: true};
"""

SUBMIT_JS = r"""
await checkIdentity();
assert(page.url().split("?")[0] === item.target || page.url().includes("/compose/"), "XTargetChanged");
if (item.action === "reply") await targetArticle(item.target);
const {editor, button, scope} = await composerControls();
if (item.action === "post") assert(await editorText(editor) === item.text, "ApprovedTextNotRetained");
else await expect(editor).toHaveText(item.text);
if (item.action === "post") {
  await expect(scope.locator('[data-testid="attachments"] img')).toHaveCount(item.media.length);
  await expect(scope.locator('[data-testid="attachments"] [role="progressbar"]')).toHaveCount(0);
}
await expect(button).toBeVisible();
await expect(button).toBeEnabled();
const receiptPromise = page.waitForResponse(r => r.request().method() === "POST" && r.url().includes("/CreateTweet"), {timeout: 25000});
await button.click();
const response = await receiptPromise;
if (response.status() === 429) return {status: "unknown", verified: false, rate_limited: true,
  retry_after_seconds: 3600, reason: "BrowserRateLimited", submission_started: true};
const data = (await response.json())?.data?.create_tweet?.tweet_results?.result;
const tweet = data?.tweet || data;
const id = tweet?.rest_id;
const parent = tweet?.legacy?.in_reply_to_status_id_str;
if (!/^\d+$/.test(id || "")) return {status: "unknown", verified: false,
  reason: "XTweetReceiptUnavailable", submission_started: true};
const legacy = tweet.legacy || {};
const media = (legacy.extended_entities || legacy.entities || {}).media || [];
const targetVerified = item.action === "post" ? !!(legacy.created_at && "full_text" in legacy &&
  !parent && !legacy.quoted_status_id_str && !legacy.is_quote_status && media.length === item.media.length && media.every(m=>m.type === "photo"))
  : parent === new URL(item.target).pathname.split("/").pop();
return {status: "unknown", verified: false, external_reference: `https://x.com/${expected}/status/${id}`,
  target_verified: targetVerified, submission_started: true};
"""

VERIFY_JS = r"""
await page.goto(receipt.external_reference, {waitUntil: "domcontentloaded"});
await checkIdentity();
const article = await targetArticle(receipt.external_reference);
const showOriginal = article.getByRole("button", {name:"Show original", exact:true});
if (await showOriginal.count() === 1) await showOriginal.click();
const authors = [...new Set([...(await article.getByTestId("User-Name").first().innerText())
  .matchAll(/@([A-Za-z0-9_]{1,15})\b/g)].map(m => m[1].toLowerCase()))];
const text = await article.getByTestId("tweetText").count() ? await tweetText(article.getByTestId("tweetText")) : "";
const stamp = await article.locator("time").getAttribute("datetime");
const mediaVerified = item.action !== "post" || await article.getByTestId("tweetPhoto").count() >= item.media.length;
const verified = receipt.target_verified === true && authors.length === 1 && authors[0] === expected && text === item.text && mediaVerified && !!stamp;
return {...receipt, status: verified ? "confirmed" : "unknown", verified, published_at: verified ? stamp : null};
"""


def reply(settings, operation, payload, checkpoint, authorize):
    """Prepare without posting, reauthorize, submit once, and retain uncertain receipts."""
    item = payload["payload"]
    if item["action"] not in {"reply", "post"} or (item["action"] == "reply" and item.get("media")):
        raise BackendError("TabbitReplyOnly")
    expected, instance = settings.get("identity", ""), settings.get("tabbit_instance", "")
    if not re.fullmatch(r"[a-z0-9_]{1,15}", expected) or not re.fullmatch(
        r"[0-9A-F]{16}", instance
    ):
        raise BackendError("InvalidTabbitConfiguration")
    if item["action"] == "post":
        if not item.get("target_owned") or not owned_x_target(item["target"], expected):
            raise BackendError("XOwnedTargetRequired")
        if len(item.get("media", [])) > 4 or any(
            m.get("mime_type") not in {"image/png", "image/jpeg"} for m in item.get("media", [])
        ):
            raise BackendError("XPostMediaInvalid")
    elif not re.fullmatch(r"https://x\.com/[A-Za-z0-9_]+/status/[0-9]+", item["target"]):
        raise BackendError("InvalidXTarget")
    prior = payload.get("receipt") or payload.get("result") or {}
    if (
        operation == "verify"
        and prior.get("reason") == "TabbitReplyUnavailable"
        and prior.get("execution_step") == "reply_composer"
        and prior.get("submission_started") is False
        and not prior.get("external_reference")
    ):
        # PREPARE_JS contains no submit action. Its settled failure is conclusive
        # negative evidence, and reconciliation never makes this item retryable.
        return {
            **prior,
            "status": "failed_known",
            "verified": True,
            "reason": "TabbitPreparationFailedBeforeSubmission",
        }
    task = "X " + item["action"] + " " + payload["item_id"]
    inventory = cli(["create", "--task", task], instance)
    if inventory.get("task", {}).get("reused"):
        raise BackendError("TabbitCheckPending")
    settled = True
    prefix = (
        "const expected = "
        + json.dumps(expected)
        + "; const item = "
        + json.dumps(item)
        + ";\n"
        + IDENTITY_JS
    )

    def program(name, code):
        nonlocal settled
        request_id = name + "-" + uuid.uuid4().hex
        settled = False
        response = cli(
            ["nodejs", "--task", task, "--request-id", request_id, "--timeout-ms", "60000"],
            instance,
            code=prefix + code,
        )
        if response.get("status") in {"queued", "running"}:
            response = cli(
                ["receipt", "--task", task, "--request-id", request_id, "--wait-ms", "60000"],
                instance,
            )
        settled = response.get("status") in {"succeeded", "failed", "interrupted"}
        if not settled:
            raise BackendError("TabbitCheckPending")
        if response.get("status") != "succeeded":
            raise BackendError("TabbitReplyUnavailable")
        if response.get("result", {}).get("resourceId"):
            raise BackendError("TabbitUnexpectedResource")
        return response.get("result", {}).get("value", {})

    progress = getattr(checkpoint, "progress", lambda *args: None)
    try:
        receipt = payload.get("receipt") or payload.get("result") or {}
        if operation == "execute":
            progress("reply_composer", False)
            try:
                prepared = program("prepare", PREPARE_JS)
            except BackendError as exc:
                if exc.safe_code != "TabbitReplyUnavailable":
                    raise
                return {
                    "status": "failed_known",
                    "verified": False,
                    "reason": "TabbitPreparationFailedBeforeSubmission",
                    "submission_started": False,
                }
            if prepared.get("prepared") is not True:
                raise BackendError("TabbitReplyUnavailable")
            authorize()
            progress("submit_click", True)
            receipt = program("submit", SUBMIT_JS)
            checkpoint(receipt)
        reference = receipt.get("external_reference", "")
        if not reference:
            return {**receipt, "status": "unknown", "verified": False}
        if not re.fullmatch(
            r"https://x\.com/" + re.escape(expected) + r"/status/[0-9]+", reference
        ):
            raise BackendError("InvalidReceiptURL")
        progress("receipt_lookup", True)
        result = program("verify", "const receipt = " + json.dumps(receipt) + ";\n" + VERIFY_JS)
        result["published_at"] = observed_timestamp(result.get("published_at"))
        checkpoint(result)
        return result
    finally:
        if settled:
            finished = cli(["finish", "--task", task], instance)
            if finished.get("forcedCleanup") or finished.get("ok") is False:
                raise BackendError("TabbitFinishIncomplete")

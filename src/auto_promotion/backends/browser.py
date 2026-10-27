"""Local, isolated browser profiles for Reddit and X. Never attaches to another PC."""

from __future__ import annotations

import math
import re
import time
import uuid
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse

from ..models import digest, epoch
from .common import (
    BackendError,
    language_evidence,
    observed_timestamp,
    owned_x_target,
    profile_path,
    read_profile,
    save_profile,
)

HOME = {"reddit": "https://www.reddit.com", "x": "https://x.com"}
X_STATE_PAGE_LIMIT = 32


def read_limits(payload):
    seconds = payload.get("timeout_seconds", 30)
    limit = payload.get("read_request_limit")
    if type(seconds) not in {int, float} or not math.isfinite(seconds) or not 1 <= seconds <= 120:
        raise BackendError("InvalidBrowserReadBudget")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
        raise BackendError("InvalidBrowserReadBudget")
    return seconds, limit


def retry_after(response, *, now=None):
    """Use only bounded retry headers; never retain response headers or URLs."""
    now = time.time() if now is None else now
    try:
        headers = response.headers
        value = headers.get("retry-after", headers.get("Retry-After", ""))
        wait = (
            int(value) if str(value).isdigit() else parsedate_to_datetime(value).timestamp() - now
        )
        if math.isfinite(wait) and wait > 0:
            return max(1, math.ceil(wait))
    except (AttributeError, TypeError, ValueError, OverflowError):
        pass
    return 900


class BrowserReadScan:
    """One bounded DOM scan. Counts primary read attempts, never all wire requests."""

    def __init__(self, page, payload):
        seconds, self.limit = read_limits(payload)
        self.page, self.deadline = page, time.monotonic() + seconds
        self.requests, self.responses, self.blocked = 0, 0, 0
        self.resources, self.resource_seen = {}, set()
        self.state, self.reason, self.wait = None, None, 0
        self.observed_at = None

    def primary(self, request):
        parsed = urlparse(request.url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "x.com",
            "www.x.com",
            "api.x.com",
            "twitter.com",
            "www.twitter.com",
            "api.twitter.com",
        }:
            return False
        if request.is_navigation_request():
            return request.frame == self.page.main_frame and parsed.path in {
                "/",
                "/home",
                "/search",
            }
        if request.resource_type not in {"xhr", "fetch"}:
            return False
        parts = parsed.path.rstrip("/").split("/")
        return (
            "graphql" in parts and parts[-1] in {"SearchTimeline", "SearchTimelineV2"}
        ) or parsed.path in {
            "/i/api/2/search/adaptive.json",
            "/2/search/adaptive.json",
            "/1.1/search/tweets.json",
            "/i/api/1.1/search/tweets.json",
        }

    def route(self, route):
        if self.primary(route.request):
            if self.state or (self.limit is not None and self.requests >= self.limit):
                self.blocked += 1
                if not self.state:
                    self.state, self.reason = "budget_exhausted", "BrowserReadBudgetExhausted"
                route.abort("aborted")
                return
            self.requests += 1
        route.fallback()

    def resource_failure(self, request):
        if request in self.resource_seen:
            return
        self.resource_seen.add(request)
        kind = request.resource_type
        kind = kind if kind in {"image", "media", "font", "script", "stylesheet"} else "third_party"
        self.resources[kind] = self.resources.get(kind, 0) + 1

    def response(self, response):
        if self.primary(response.request):
            self.responses += 1
            if response.status == 429:
                self.state, self.reason = "rate_limited", "BrowserRateLimited"
                self.wait = max(self.wait, retry_after(response))
                self.observed_at = time.time()
        elif response.status >= 400:
            self.resource_failure(response.request)

    def failed(self, request):
        if self.primary(request):
            if not self.state:
                self.state, self.reason = "network_error", "BrowserSearchRequestFailed"
        else:
            self.resource_failure(request)

    def remaining(self, cap=15000):
        self.check()
        return max(1, min(cap, (self.deadline - time.monotonic()) * 1000))

    def check(self):
        if self.state:
            raise BackendError(self.reason)
        if time.monotonic() >= self.deadline:
            self.state, self.reason = "timeout", "BrowserReadDeadlineExceeded"
            raise BackendError(self.reason)

    def start(self):
        self.page.route("**/*", self.route)
        self.page.on("response", self.response)
        self.page.on("requestfailed", self.failed)

    def close(self):
        complete = True
        for cleanup in (
            lambda: self.page.unroute("**/*", self.route),
            lambda: self.page.remove_listener("response", self.response),
            lambda: self.page.remove_listener("requestfailed", self.failed),
        ):
            try:
                cleanup()
            except Exception:
                complete = False
        return complete

    def decorate(self, result):
        stopped = bool(
            self.state or result.get("rate_limited") or result.get("account_block_reason")
        )
        state = self.state or (
            "challenge"
            if result.get("account_block_reason")
            else "rate_limited"
            if result.get("rate_limited")
            else "partial"
        )
        if stopped:
            result.update(candidates=[], read_page_complete=False)
        else:
            result.setdefault("read_page_complete", bool(result.get("candidates")))
        values = result.get("candidates", [])
        result.update(
            read_state=state,
            coverage_complete=False,
            next_cursor=None,
            read_observed_at=self.observed_at if stopped else time.time(),
            read_observation_source="playwright_primary_response"
            if self.observed_at
            else "playwright_visible_dom",
            read_primary_request_count=self.requests,
            read_primary_response_count=self.responses,
            read_primary_requests_blocked=self.blocked,
            read_resource_failures=dict(self.resources),
            read_request_count_source="playwright_navigation_and_search_attempts",
            read_page_fingerprint=digest(
                sorted(str(row.get("id") or row.get("target") or row.get("url")) for row in values)
            )
            if result["read_page_complete"]
            else None,
        )
        if state == "rate_limited":
            result.update(rate_limited=True, retry_after_seconds=self.wait or 900)
        return result

    def failure(self, exc):
        code = self.reason or getattr(exc, "safe_code", type(exc).__name__)
        if not self.state:
            self.state = "timeout" if type(exc).__name__ == "TimeoutError" else "unobservable"
        return self.decorate({"candidates": [], "reason": code, "read_page_complete": False})


class XStateError(BackendError):
    """Only safe, categorical evidence; never includes page or response text."""

    def __init__(self, code, failure_class, account_block_reason=None, *, rejected=False):
        super().__init__(code)
        self.failure_class = failure_class
        self.account_block_reason = account_block_reason
        self.confirmed_rejected = rejected


def check_x_state(page):
    """Inspect trusted routes and visible controls, never user-authored tweet text."""
    if not isinstance(page.url, str):
        return
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
    }:
        return
    path = parsed.path.rstrip("/").casefold()
    if path in {"/account/access", "/i/flow/arkose", "/i/flow/challenge"}:
        raise XStateError("XChallengeRequired", "challenge", "x_challenge")
    if path in {"/login", "/i/flow/login"}:
        raise XStateError("XLoginRequired", "login_required", "x_login_required")
    if path in {"/account/suspended", "/account/locked"}:
        raise XStateError("XAccountRestricted", "restricted", "x_account_restricted")
    components = (
        ('iframe[src^="https://client-api.arkoselabs.com/"]', "challenge", "x_challenge"),
        ('iframe[src^="https://iframe.arkoselabs.com/"]', "challenge", "x_challenge"),
        ('iframe[src^="https://challenges.cloudflare.com/"]', "challenge", "x_challenge"),
        ("#arkoseFrame", "challenge", "x_challenge"),
        ('[data-testid="arkoseChallenge"]', "challenge", "x_challenge"),
        ('[data-testid="accountSuspended"]', "restricted", "x_account_restricted"),
        ('[data-testid="accountLocked"]', "restricted", "x_account_restricted"),
    )
    for selector, kind, block in components:
        locator = page.locator(selector)
        count = locator.count()
        if count > X_STATE_PAGE_LIMIT:
            raise BackendError("XStateObservationUnavailable")
        if any(
            locator.nth(i).is_visible()
            and locator.nth(i).evaluate("""el => {
              for (let node = el; node; node = node.parentElement) {
                const style = getComputedStyle(node);
                if (style.visibility === 'hidden' || style.visibility === 'collapse' ||
                    Number(style.opacity) === 0 || node.getAttribute('aria-hidden') === 'true')
                  return false;
              }
              return true;
            }""")
            for i in range(count)
        ):
            raise XStateError(
                "XChallengeRequired" if kind == "challenge" else "XAccountRestricted",
                kind,
                block,
            )


def check_x_context_state(context, selected):
    """Observe only this account's context; never navigate away from evidence.

    A failed observation cannot establish health. Continue past unreadable pages
    so reliable challenge evidence on another page still takes precedence.
    """
    incomplete = False
    try:
        pages = context.pages if context is not None else [selected]
        if not isinstance(pages, (list, tuple)) or len(pages) > X_STATE_PAGE_LIMIT:
            raise BackendError("XStateObservationUnavailable")
        pages = list(pages)
        if selected not in pages:
            raise BackendError("XStateObservationUnavailable")
    except Exception:
        raise BackendError("XStateObservationUnavailable") from None
    for page in pages:
        try:
            closed = getattr(page, "is_closed", lambda: False)() is True
            if closed:
                incomplete |= page is selected
                continue
            before = page.url
            check_x_state(page)
            # Recheck the route after DOM observation; a navigation can change
            # the route to a challenge while locators are being inspected.
            after = page.url
            if after != before:
                check_x_state(page)
                incomplete = True
        except XStateError:
            raise
        except Exception:
            # A sibling proven closed is no longer an observable account page.
            # Unknown/failed closure checks remain incomplete, never healthy.
            try:
                closed = getattr(page, "is_closed", lambda: False)() is True
            except Exception:
                closed = False
            incomplete |= page is selected or not closed
    if context is not None:
        try:
            current = context.pages
            incomplete |= (
                not isinstance(current, (list, tuple))
                or len(current) > X_STATE_PAGE_LIMIT
                or selected not in current
                or any(page not in pages for page in current)
            )
        except Exception:
            incomplete = True
    try:
        incomplete |= getattr(selected, "is_closed", lambda: False)() is True
    except Exception:
        incomplete = True
    if incomplete:
        raise BackendError("XStateObservationUnavailable")


def legacy_x_state_error(exc):
    """Bridge exact trusted helper codes, never exception text or page prose."""
    signals = {
        "BrowserVerificationRequired": ("challenge", "x_challenge"),
        "BrowserAccountRestricted": ("restricted", "x_account_restricted"),
        "BrowserLoginRequired": ("login_required", "x_login_required"),
    }
    code = getattr(exc, "safe_code", None)
    if isinstance(exc, BackendError) and code in signals:
        kind, block = signals[code]
        return XStateError(code, kind, block, rejected=getattr(exc, "confirmed_rejected", False))
    return exc


def x_response_error(document, status):
    """Classify structured API codes, not message substrings or tweet content."""
    errors = document.get("errors", []) if isinstance(document, dict) else []
    codes = set()
    for error in errors if isinstance(errors, list) else []:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions") or {}
        if not isinstance(extensions, dict):
            extensions = {}
        for value in (error.get("code"), extensions.get("code"), error.get("type")):
            if isinstance(value, (str, int)):
                codes.add(str(value).upper())
    if codes & {"CHALLENGE_REQUIRED", "CAPTCHA_REQUIRED", "ARKOSE_CHALLENGE_REQUIRED"}:
        return XStateError("XChallengeRequired", "challenge", "x_challenge", rejected=True)
    if codes & {"64", "226", "326", "ACCOUNT_LOCKED", "ACCOUNT_SUSPENDED"}:
        return XStateError(
            "XAccountRestricted", "restricted", "x_account_restricted", rejected=True
        )
    if status == 401 or codes & {"32", "89", "215", "AUTHENTICATION_REQUIRED", "UNAUTHORIZED"}:
        return XStateError("XLoginRequired", "login_required", "x_login_required", rejected=True)
    if status == 429 or codes & {"88", "185", "RATE_LIMITED", "TOO_MANY_REQUESTS"}:
        return XStateError("BrowserRateLimited", "rate_limited", rejected=True)
    if status == 403:
        return XStateError("XHTTPForbidden", "http_forbidden", rejected=True)
    if errors:
        return XStateError("XResponseRejected", "response_rejected", rejected=True)
    return None


def parse_create_tweet(response):
    try:
        document = response.json()
    except Exception:
        document = {}
    data = document if isinstance(document, dict) else {}
    for key in ("data", "create_tweet", "tweet_results", "result"):
        data = data.get(key) if isinstance(data, dict) else None
    data = data or {}
    if isinstance(data, dict):
        data = data.get("tweet", data)
    if isinstance(data, dict) and str(data.get("rest_id", "")).isdigit():
        # A durable reference must be saved before any later page-state failure.
        return data
    error = x_response_error(document, response.status)
    if error:
        raise error
    raise BackendError("XTweetReceiptUnavailable")


def launch_context(runtime, root, *, browser_channel="chromium", headless):
    if browser_channel not in {"chromium", "msedge", "chrome"}:
        raise BackendError("UnsupportedBrowserChannel")
    options = {"channel": browser_channel} if browser_channel != "chromium" else {}
    directory = "browser" if browser_channel == "chromium" else "browser-" + browser_channel
    return runtime.chromium.launch_persistent_context(
        str(root / directory),
        headless=headless,
        locale="en-US",
        viewport={"width": 1440, "height": 1000},
        **options,
    )


def check_response(response):
    if response is not None and response.status == 429:
        raise BackendError("BrowserRateLimited")
    return response


def identity(platform, context, page, *, timeout=15000):
    if platform == "reddit":
        response = context.request.get(HOME[platform] + "/api/me.json")
        check_response(response)
        if not response.ok:
            raise BackendError("RedditIdentityUnavailable")
        return str(response.json().get("data", {}).get("name", "")).casefold()
    switcher = page.get_by_test_id("SideNav_AccountSwitcher_Button")
    switcher.wait_for(timeout=timeout)
    handles = set(re.findall(r"@([A-Za-z0-9_]{1,15})\b", switcher.inner_text()))
    if len(handles) != 1:
        raise BackendError("XIdentityUnavailable")
    return handles.pop().casefold()


def login(platform, label, *, browser_channel="chromium"):
    from playwright.sync_api import sync_playwright

    expected = input("Platform username (without @ or u/): ").strip().casefold()
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", expected):
        raise BackendError("InvalidPlatformUsername")
    root = profile_path(platform, label)
    root.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as runtime:
        context = launch_context(runtime, root, browser_channel=browser_channel, headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                HOME[platform] + ("/login" if platform == "reddit" else "/i/flow/login"),
                wait_until="domcontentloaded",
            )
            input("Finish login in the opened browser, then press Enter here: ")
            check_response(page.goto(HOME[platform], wait_until="domcontentloaded"))
            observed = identity(platform, context, page)
            if not observed or observed != expected:
                raise BackendError("BrowserIdentityMismatch")
            save_profile(
                platform, label, {"identity": observed, "browser_channel": browser_channel}
            )
            return {"platform": platform, "account": label, "authenticated": True}
        finally:
            context.close()


def visible_one(locator):
    visible = [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]
    if len(visible) != 1:
        raise BackendError("BrowserControlNotUnambiguouslyAvailable")
    return visible[0]


def reddit_record(tree, identifier):
    if isinstance(tree, list):
        for branch in tree:
            found = reddit_record(branch, identifier)
            if found:
                return found
    elif isinstance(tree, dict):
        if tree.get("kind") in {"t1", "t3"}:
            data = tree.get("data", {})
            if data.get("id") == identifier:
                return data
        for value in tree.values():
            if isinstance(value, (dict, list)):
                found = reddit_record(value, identifier)
                if found:
                    return found
    return None


class BrowserBackend:
    def __init__(self, platform, label, context, page, settings):
        self.platform, self.label = platform, label
        self.context, self.page, self.settings = context, page, settings
        self.execution_step = "browser_start"
        self.submission_started = False
        self.progress = None
        self.response_failure = None

    def step(self, value, submission_started=None):
        self.execution_step, self.step_started_at = value, time.time()
        if submission_started is not None:
            self.submission_started = submission_started
        if self.progress:
            self.progress(value, self.submission_started)

    def diagnostics(self):
        return {
            "execution_step": self.execution_step,
            "step_started_at": self.step_started_at,
            "submission_started": self.submission_started,
        }

    def check_identity(self):
        self.check_state()
        observed = identity(
            self.platform,
            self.context,
            self.page,
            **({"timeout": self.read_timeout()} if getattr(self, "read_scan", None) else {}),
        )
        self.check_state()
        if not observed or observed != self.settings["identity"]:
            raise BackendError("BrowserIdentityMismatch")

    def read_timeout(self, cap=15000):
        scan = getattr(self, "read_scan", None)
        return scan.remaining(cap) if scan else cap

    def wait_read_results(self, locator):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        deadline = time.monotonic() + self.read_timeout() / 1000
        while time.monotonic() < deadline:
            self.check_state()
            try:
                locator.wait_for(timeout=min(250, self.read_timeout()))
                if getattr(self, "read_scan", None):
                    self.read_scan.check()
                self.check_state()
                return True
            except PlaywrightTimeoutError:
                pass
        return False

    def check_state(self):
        if self.platform == "x":
            check_x_context_state(self.context, self.page)

    def navigate_x(self, url):
        response = self.page.goto(
            url,
            wait_until="domcontentloaded",
            **({"timeout": self.read_timeout()} if getattr(self, "read_scan", None) else {}),
        )
        self.check_state()
        if getattr(self, "read_scan", None):
            self.read_scan.check()
        if response is not None and response.status in {401, 403, 429}:
            raise x_response_error({}, response.status)
        if response is not None and response.status >= 500:
            raise BackendError("BrowserNavigationServerError")
        return response

    def create_tweet_data(self, response):
        data = parse_create_tweet(response)
        # Even a partial GraphQL success can carry an account restriction.
        # Save its accepted ID before reporting that independent account state.
        try:
            error = x_response_error(response.json(), response.status)
        except Exception:
            error = None
        if error and error.account_block_reason:
            self.response_failure = error
        return data

    def run(self, operation, payload, checkpoint, authorize):
        self.read_scan = None
        self.read_guard_pending = False
        if self.platform != "x" or operation != "discover":
            return self._run_guarded(operation, payload, checkpoint, authorize)
        self.read_scan = BrowserReadScan(self.page, payload)
        self.read_scan.start()
        result = None
        try:
            try:
                result = self._run_guarded(operation, payload, checkpoint, authorize)
            except Exception as exc:
                if self.read_guard_pending:
                    raise
                result = self.read_scan.failure(exc)
                return result
            result = self.read_scan.decorate(result)
            return result
        finally:
            if not self.read_scan.close() and result is not None:
                result["read_cleanup_state"] = "incomplete"
            self.read_scan = None

    def _run_guarded(self, operation, payload, checkpoint, authorize):
        if self.platform == "x":
            # A receipt preserves delivery evidence, not a fresh observation of
            # account state. Replaying its guard event could keep an old recovery
            # token valid when this operation detects a new challenge.
            payload = dict(payload)
            for key in ("receipt", "result"):
                if isinstance(payload.get(key), dict):
                    payload[key] = {
                        field: value
                        for field, value in payload[key].items()
                        if not field.startswith("account_block_")
                        and field not in {"account_restriction", "browser_page_anomaly"}
                    }
        self.progress = getattr(checkpoint, "progress", None) if operation == "execute" else None
        self.submission_started = bool(
            (payload.get("receipt") or payload.get("result") or {}).get("submission_started")
        )
        self.step("browser_ready")
        self.response_failure = None
        receipt = dict(payload.get("receipt") or payload.get("result") or {})
        for key in ("external_reference", "message_ids"):
            if payload.get(key) and not receipt.get(key):
                receipt[key] = payload[key]
        save = checkpoint

        def checkpoint(result):
            receipt.update(result)
            save(result)

        checkpoint.progress = self.progress
        try:
            result = self._run(operation, payload, checkpoint, authorize)
            if operation in {"execute", "verify"}:
                if result.get("verified") is True:
                    self.step("completed")
                result.update(self.diagnostics())
            return result
        except Exception as exc:
            if self.platform == "x":
                exc = legacy_x_state_error(exc)
                # A challenge may have appeared while a control wait timed out.
                try:
                    self.check_state()
                except XStateError as state:
                    # Preserve an explicit CreateTweet refusal if both exist.
                    state.confirmed_rejected = getattr(exc, "confirmed_rejected", False)
                    exc = state
                except Exception:
                    # A closed page cannot provide new state evidence.
                    pass
                code = getattr(exc, "safe_code", type(exc).__name__)
                kind = getattr(exc, "failure_class", None)
                if kind is None:
                    if code == "BrowserNavigationAccessDenied":
                        kind = "http_forbidden"
                    elif code == "BrowserRateLimited":
                        kind = "rate_limited"
                    elif type(exc).__name__ == "TimeoutError":
                        kind = (
                            "network"
                            if self.execution_step == "submission_response"
                            else "page_anomaly"
                        )
                    elif (
                        isinstance(exc, OSError)
                        or "net::ERR_" in str(exc)
                        or code
                        in {
                            "BrowserNavigationUnavailable",
                            "BrowserNavigationDownload",
                            "BrowserNavigationServerError",
                        }
                    ):
                        kind = "network"
                    elif code in {"BrowserIdentityMismatch", "XIdentityUnavailable"}:
                        kind = "identity_mismatch"
                    elif isinstance(exc, BackendError) and code not in {
                        "BrowserReplyControlUnavailable",
                        "BrowserReplyComposerAmbiguous",
                        "BrowserControlNotUnambiguouslyAvailable",
                        "BrowserNavigationTimeout",
                        "XTweetReceiptUnavailable",
                    }:
                        kind = "preparation_failed"
                    else:
                        kind = "page_anomaly"
                block = getattr(exc, "account_block_reason", None)
                if operation != "execute" and not block and kind != "rate_limited":
                    exc.execution_step = self.execution_step
                    exc.submission_started = self.submission_started
                    raise exc
                durable = bool(receipt.get("external_reference") or receipt.get("message_ids"))
                not_submitted = (
                    operation == "execute" and not self.submission_started and not durable
                )
                rejected = (
                    operation == "execute"
                    and bool(getattr(exc, "confirmed_rejected", False))
                    and not durable
                )
                result = {
                    **receipt,
                    "status": "failed_known" if not_submitted or rejected else "unknown",
                    "verified": False,
                    "reason": code,
                    "failure_class": kind,
                    "confirmed_not_submitted": not_submitted,
                    "confirmed_rejected": rejected,
                    **self.diagnostics(),
                }
                if block:
                    result["account_block_reason"] = block
                    # Generate once at the actual detection boundary; native
                    # checkpoint and Engine then carry and deduplicate this ID.
                    result["account_block_event_id"] = uuid.uuid4().hex
                if kind == "page_anomaly" and operation == "execute":
                    result["browser_page_anomaly"] = True
                if kind == "rate_limited":
                    result.update(rate_limited=True, retry_after_seconds=3600)
                if operation == "health":
                    result.update(healthy=False, service_ready=False, identity_verified=False)
                elif operation == "preflight":
                    result["allowed"] = False
                if operation == "execute" or block:
                    self.read_guard_pending = bool(block)
                    checkpoint(result)
                return result
            exc.execution_step = self.execution_step
            exc.submission_started = self.submission_started
            if isinstance(exc, BackendError) and exc.safe_code == "BrowserRateLimited":
                return {
                    "status": "unknown",
                    "verified": False,
                    "rate_limited": True,
                    "retry_after_seconds": 3600,
                    "reason": "BrowserRateLimited",
                    **self.diagnostics(),
                }
            if (
                operation == "execute"
                and payload.get("payload", {}).get("action") == "post"
                and not self.submission_started
            ):
                return {
                    "status": "failed_known",
                    "verified": False,
                    "reason": getattr(exc, "safe_code", type(exc).__name__),
                    **self.diagnostics(),
                }
            raise

    def _run(self, operation, payload, checkpoint, authorize):
        self.step("account_state_check")
        self.check_state()
        self.step("home_navigation")
        self.navigate_home()
        self.step("identity_check")
        self.check_identity()
        if operation == "health":
            from ..models import CAPABILITIES

            return {
                "healthy": True,
                "service_ready": True,
                "identity_verified": True,
                "account_identity": self.settings["identity"],
                "reason": "account_read_verified",
                "permissions": sorted(CAPABILITIES[self.platform]),
            }
        if operation == "preflight":
            return {"allowed": True}
        if operation == "discover":
            if payload.get("high_frequency"):
                if self.platform != "x" or payload.get("read_only") is not True:
                    raise BackendError("BrowserHighFrequencyReadOnlyRequired")
                return self.hf_discover(payload)
            return self.discover(payload["policy"])
        if operation == "verify":
            return self.verify(payload)
        if operation != "execute":
            raise BackendError("UnsupportedBrowserOperation")
        return (self.reddit_execute if self.platform == "reddit" else self.x_execute)(
            payload, checkpoint, authorize
        )

    def navigate_home(self):
        # Navigation may throw "Download is starting" before goto returns its
        # HTTP response. Retain only the navigation status, never URLs/headers.
        statuses = []

        def observed(response):
            if (
                response.request.is_navigation_request()
                and urlparse(response.url).hostname == urlparse(HOME[self.platform]).hostname
                and isinstance(response.status, int)
            ):
                statuses.append(response.status)
                del statuses[:-4]

        def failure(status):
            if status in {401, 403}:
                return "BrowserNavigationAccessDenied"
            if status == 429:
                return "BrowserRateLimited"
            if status is not None and status >= 500:
                return "BrowserNavigationServerError"
            return None

        self.page.on("response", observed)
        try:
            try:
                response = self.page.goto(
                    HOME[self.platform],
                    wait_until="domcontentloaded",
                    **(
                        {"timeout": self.read_timeout()} if getattr(self, "read_scan", None) else {}
                    ),
                )
            except Exception as exc:
                code = failure(statuses[-1] if statuses else None)
                if code is None:
                    code = (
                        "BrowserNavigationTimeout"
                        if type(exc).__name__ == "TimeoutError"
                        else "BrowserNavigationDownload"
                        if "Download is starting" in str(exc)
                        else "BrowserNavigationUnavailable"
                    )
                raise BackendError(code) from None
            code = failure(response.status if response is not None else None)
            self.check_state()
            if self.platform == "x" and response is not None and response.status in {401, 403, 429}:
                raise x_response_error({}, response.status)
            if code:
                raise BackendError(code)
            return response
        finally:
            self.page.remove_listener("response", observed)

    def discover(self, policy):
        records = {}
        maximum = policy["max_candidates"]
        for query in dict.fromkeys(policy["queries"]):
            if self.platform == "reddit":
                response = self.context.request.get(
                    HOME["reddit"]
                    + "/search.json?q="
                    + quote(query)
                    + f"&limit={maximum}&type=link"
                )
                check_response(response)
                if not response.ok:
                    raise BackendError("RedditSearchUnavailable")
                for wrapper in response.json().get("data", {}).get("children", []):
                    row = wrapper["data"]
                    if row.get("over_18"):
                        continue
                    text = row.get("selftext", "")
                    language, confidence = language_evidence(row.get("title", "") + " " + text)
                    target = HOME["reddit"] + row["permalink"]
                    records[target] = {
                        "target": target,
                        "title": row.get("title", ""),
                        "text": text,
                        "language": language,
                        "confidence": confidence,
                        "activity": row.get("num_comments", 0),
                        "is_public": True,
                    }
            else:
                self.navigate_x(HOME["x"] + "/search?q=" + quote(query) + "&src=typed_query&f=live")
                parsed = urlparse(self.page.url)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname not in {"x.com", "www.x.com"}
                    or parsed.path != "/search"
                ):
                    raise BackendError("BrowserSearchUnavailable")
                articles = self.page.locator('[data-testid="primaryColumn"] [data-testid="tweet"]')
                if not self.wait_read_results(articles.first):
                    break
                for index in range(min(articles.count(), maximum)):
                    article = articles.nth(index)
                    links = article.locator('a[href*="/status/"]').filter(
                        has=self.page.locator("time")
                    )
                    if links.count() != 1:
                        continue
                    path = links.first.get_attribute("href")
                    if not re.fullmatch(r"/[A-Za-z0-9_]+/status/\d+", path or ""):
                        continue
                    text = article.get_by_test_id("tweetText").first.inner_text()
                    language, confidence = language_evidence(text)
                    target = HOME["x"] + path
                    records[target] = {
                        "target": target,
                        "text": text,
                        "language": language,
                        "confidence": confidence,
                        "activity": 0,
                        "is_public": True,
                    }
            if len(records) >= maximum:
                break
        return {
            "candidates": list(records.values())[:maximum],
            "source_note": "浏览器公开搜索；无法证明语言、活跃度或置信度时保守拒绝",
        }

    def hf_discover(self, payload):
        """Observe the visible live results only; never claim complete time coverage."""
        if payload.get("cursor"):
            raise BackendError("BrowserSearchCursorUnavailable")
        query = payload["query"]
        self.navigate_x(HOME["x"] + "/search?q=" + quote(query) + "&src=typed_query&f=live")
        parsed = urlparse(self.page.url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"x.com", "www.x.com"}
            or parsed.path != "/search"
        ):
            raise BackendError("BrowserSearchUnavailable")
        # Search results belong to the primary column. Other visible tweets can
        # appear in side panels and must not be admitted as query matches.
        articles = self.page.locator('[data-testid="primaryColumn"] [data-testid="tweet"]')
        if not self.wait_read_results(articles.first):
            return {
                "candidates": [],
                "next_cursor": None,
                "coverage_complete": False,
                "reason": "browser_results_not_observable",
            }
        observed = []
        for index in range(min(articles.count(), 100)):
            article = articles.nth(index)
            links = article.locator('a[href*="/status/"]').filter(has=self.page.locator("time"))
            if links.count() != 1:
                continue
            path = links.first.get_attribute("href") or ""
            match = re.fullmatch(r"/([A-Za-z0-9_]+)/status/(\d+)", path)
            texts = article.get_by_test_id("tweetText")
            times = links.first.locator("time")
            if not match or texts.count() != 1 or times.count() != 1:
                continue
            when = times.first.get_attribute("datetime")
            if not when:
                continue
            try:
                at = epoch(when)
            except ValueError:
                continue
            if not payload["lower_at"] <= at <= payload["upper_at"]:
                continue
            content = texts.first.inner_text()
            if not content.strip():
                continue
            language, _ = language_evidence(content)
            observed.append(
                {
                    "id": match[2],
                    "url": HOME["x"] + "/i/web/status/" + match[2],
                    "text": content,
                    "author_id": "@" + match[1],
                    "source_event_at": at,
                    "context_status": "missing",
                    "context": "",
                    "language": language,
                    "query_visible_text": article.inner_text()[:2000],
                }
            )
        self.check_state()
        if getattr(self, "read_scan", None):
            self.read_scan.check()
        return {
            "candidates": observed,
            "next_cursor": None,
            "coverage_complete": False,
            "reason": "browser_window_not_exhaustive",
        }

    def reddit_execute(self, payload, checkpoint, authorize):
        item = payload["payload"]
        page = self.page
        if item["action"] == "post":
            subreddit = item["target"].removeprefix("r/")
            if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit):
                raise BackendError("InvalidSubreddit")
            check_response(page.goto(HOME["reddit"] + f"/r/{subreddit}/submit"))
            title = page.locator(
                'textarea[name="title"], input[name="title"], textarea[placeholder="Title"]'
            )
            visible_one(title).fill(item["title"])
            body = page.locator(
                'div[contenteditable="true"][role="textbox"], textarea[name="text"]'
            )
            visible_one(body).fill(item["text"])
            button = visible_one(page.get_by_role("button", name="Post", exact=True))
            self.check_identity()
            authorize()
            button.click()
            page.wait_for_url(
                re.compile(r"https://www\.reddit\.com/r/[^/]+/comments/[a-z0-9]+/.*"), timeout=25000
            )
            url = page.url.split("?")[0]
        elif item["action"] == "reply":
            parsed = urlparse(item["target"])
            if parsed.hostname not in {"www.reddit.com", "reddit.com"} or parsed.scheme != "https":
                raise BackendError("InvalidRedditTarget")
            check_response(page.goto(item["target"]))
            path_parts = parsed.path.strip("/").split("/")
            comment_id = path_parts[5] if len(path_parts) > 5 else None
            if comment_id:
                target = page.locator(f'shreddit-comment[thingid="t1_{comment_id}"]')
                visible_one(target.get_by_role("button", name="Reply", exact=True)).click()
                scope = target
            else:
                scope = page.locator("shreddit-comment-composer")
                if scope.count() != 1:
                    raise BackendError("RedditCommentComposerUnavailable")
            visible_one(scope.locator('div[contenteditable="true"], textarea')).fill(item["text"])
            button = visible_one(scope.get_by_role("button", name="Comment", exact=True))
            self.check_identity()
            authorize()
            with page.expect_response(
                lambda r: r.request.method == "POST" and "/api/comment" in r.url, timeout=25000
            ) as accepted:
                button.click()
            result = (
                check_response(accepted.value)
                .json()
                .get("json", {})
                .get("data", {})
                .get("things", [])
            )
            if len(result) != 1 or result[0].get("kind") != "t1":
                raise BackendError("RedditCommentReceiptUnavailable")
            data = result[0]["data"]
            expected_parent = "t1_" + comment_id if comment_id else "t3_" + path_parts[3]
            if data.get("parent_id") != expected_parent:
                raise BackendError("RedditReplyTargetMismatch")
            permalink = data.get("permalink")
            if not permalink or not permalink.startswith("/r/"):
                raise BackendError("RedditCommentReceiptUnavailable")
            url = HOME["reddit"] + permalink
        else:
            raise BackendError("UnsupportedRedditAction")
        result = {"status": "unknown", "verified": False, "external_reference": url}
        checkpoint(result)
        return self.verify({**payload, "receipt": result})

    def tweet(self, url):
        status_id = urlparse(url).path.rstrip("/").split("/")[-1]
        if not status_id.isdigit():
            raise BackendError("InvalidXTarget")
        articles = self.page.get_by_test_id("tweet")
        article = articles.filter(has=self.page.locator(f'a[href$="/status/{status_id}"] time'))
        article.first.wait_for(timeout=15000)
        return visible_one(article)

    def x_execute(self, payload, checkpoint, authorize):
        item = payload["payload"]
        if item["action"] == "post":
            return self.x_original_post(payload, checkpoint, authorize)
        page = self.page
        parsed = urlparse(item["target"])
        if parsed.scheme != "https" or parsed.hostname not in {
            "x.com",
            "www.x.com",
            "twitter.com",
            "www.twitter.com",
        }:
            raise BackendError("InvalidXTarget")
        self.step("target_navigation")
        self.navigate_x(item["target"])
        self.step("target_lookup")
        article = self.tweet(item["target"])
        self.step("identity_check")
        self.check_identity()
        action = item["action"]
        if action in {"like", "repost"}:
            done = "unlike" if action == "like" else "unretweet"
            if article.get_by_test_id(done).count() == 0:
                self.check_state()
                authorize()
                self.check_state()
                self.step("submit_click", True)
                visible_one(
                    article.get_by_test_id("like" if action == "like" else "retweet")
                ).click()
                if action == "repost":
                    self.check_state()
                    authorize()
                    self.check_state()
                    visible_one(page.get_by_test_id("retweetConfirm")).click()
            self.step("submission_response")
            article.get_by_test_id(done).wait_for(timeout=15000)
            result = {"status": "unknown", "verified": False, "external_reference": item["target"]}
            checkpoint(result)
            return self.verify({**payload, "receipt": result})
        if action == "reply":
            self.step("reply_control")
            # X can render a second reply control far below the target tweet.
            # The first control belongs to the target article's action bar.
            reply_control = article.get_by_test_id("reply").first
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            try:
                # The article can render before its action bar. Wait for this
                # exact control once; do not retry a submission or pick another tweet.
                deadline = time.monotonic() + 15
                reply_control.wait_for(state="visible", timeout=15000)
                remaining = (deadline - time.monotonic()) * 1000
                if remaining <= 0:
                    raise BackendError("BrowserReplyControlUnavailable")
                reply_control.click(timeout=max(1, remaining))
            except PlaywrightTimeoutError:
                raise BackendError("BrowserReplyControlUnavailable") from None
            self.step("reply_composer")
            # X uses either an inline composer or a modal, depending on the
            # current page layout. A hidden dialog shell may coexist with the
            # inline composer, so select only the active modal when present.
            # The background inline editor can stay visible while a modal opens.
            page.wait_for_timeout(1500)
            page.locator('[data-testid="tweetTextarea_0"]:visible').first.wait_for(timeout=15000)
            active_dialogs = [
                page.get_by_role("dialog").nth(i)
                for i in range(page.get_by_role("dialog").count())
                if page.get_by_role("dialog").nth(i).is_visible()
                and page.get_by_role("dialog").nth(i).get_by_test_id("tweetTextarea_0").count()
            ]
            if len(active_dialogs) > 1:
                raise BackendError("BrowserReplyComposerAmbiguous")
            if active_dialogs:
                composer = active_dialogs[0]
                submit_button = composer.get_by_test_id("tweetButton")
            else:
                composer = page
                submit_button = page.get_by_test_id("tweetButtonInline")
        elif action == "quote":
            visible_one(article.get_by_test_id("retweet")).click()
            visible_one(page.get_by_role("menuitem", name="Quote", exact=True)).click()
            composer = visible_one(page.get_by_role("dialog"))
            submit_button = composer.get_by_test_id("tweetButton")
        else:
            raise BackendError("UnsupportedXAction")
        self.step("fill_reply")
        self.check_state()
        editor = visible_one(composer.get_by_test_id("tweetTextarea_0"))
        editor.click()
        editor.press("Control+A")
        editor.press("Backspace")
        editor.press_sequentially(item["text"])
        if editor.inner_text() != item["text"]:
            raise BackendError("ApprovedTextNotRetained")
        if item["media"]:
            self.step("attach_media")
            composer.locator('input[type="file"]').set_input_files(
                [m["path"] for m in item["media"]]
            )
        self.step("submission_authorization")
        self.check_state()
        authorize()
        self.check_state()
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/CreateTweet" in r.url, timeout=25000
        ) as accepted:
            self.step("submit_click", True)
            visible_one(submit_button).click()
            self.step("submission_response")
        self.step("parse_receipt")
        data = self.create_tweet_data(accepted.value)
        tweet_id = str(data.get("rest_id", ""))
        if not tweet_id.isdigit():
            raise BackendError("XTweetReceiptUnavailable")
        legacy = data.get("legacy", {})
        target_id = parsed.path.rstrip("/").split("/")[-1]
        relation = legacy.get(
            "in_reply_to_status_id_str" if action == "reply" else "quoted_status_id_str"
        )
        url = f"https://x.com/{self.settings['identity']}/status/{tweet_id}"
        result = {
            "status": "unknown",
            "verified": False,
            "external_reference": url,
            "target_verified": str(relation) == target_id,
        }
        checkpoint(result)
        self.step("receipt_saved")
        if self.response_failure:
            raise self.response_failure
        self.check_state()
        return self.verify({**payload, "receipt": result})

    def x_original_post(self, payload, checkpoint, authorize):
        from playwright.sync_api import expect

        item, page = payload["payload"], self.page
        if not item.get("target_owned") or not owned_x_target(
            item["target"], self.settings["identity"]
        ):
            raise BackendError("XOwnedTargetRequired")
        self.step("post_composer", False)
        self.navigate_x("https://x.com/compose/post")
        self.check_identity()
        editor = page.locator('[data-testid="tweetTextarea_0"]:visible')
        editor.first.wait_for(state="visible", timeout=15000)
        editor = visible_one(editor)
        dialogs = page.locator('[role="dialog"]:visible').filter(has=editor)
        composer = visible_one(dialogs) if dialogs.count() else page
        if (
            editor.inner_text()
            or composer.locator(
                '[data-testid="attachments"] img, [data-testid="attachments"] video'
            ).count()
        ):
            raise BackendError("XComposerNotEmpty")
        button = composer.get_by_test_id("tweetButton" if dialogs.count() else "tweetButtonInline")
        editor.click()
        editor.press_sequentially(item["text"])
        if editor.inner_text() != item["text"]:
            raise BackendError("ApprovedTextNotRetained")
        if item["media"]:
            self.step("attach_media", False)
            composer.locator('input[type="file"]').set_input_files(
                [m["path"] for m in item["media"]]
            )
        expect(composer.locator('[data-testid="attachments"] img')).to_have_count(
            len(item["media"]), timeout=45000
        )
        expect(composer.locator('[data-testid="attachments"] [role="progressbar"]')).to_have_count(
            0, timeout=45000
        )
        expect(button).to_be_enabled(timeout=45000)
        self.check_identity()
        if editor.inner_text() != item["text"]:
            raise BackendError("ApprovedTextNotRetained")
        self.step("submission_authorization", False)
        self.check_state()
        authorize()
        self.check_state()
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/CreateTweet" in r.url, timeout=25000
        ) as accepted:
            self.step("submit_click", True)
            visible_one(button).click()
        self.step("parse_receipt", True)
        data = self.create_tweet_data(accepted.value)
        tweet_id, legacy = str(data.get("rest_id", "")), data.get("legacy", {})
        if not tweet_id.isdigit():
            raise BackendError("XTweetReceiptUnavailable")
        media = legacy.get("extended_entities", legacy.get("entities", {})).get("media", [])
        original = bool(
            legacy.get("created_at")
            and "full_text" in legacy
            and not legacy.get("in_reply_to_status_id_str")
            and not legacy.get("quoted_status_id_str")
            and not legacy.get("is_quote_status")
            and len(media) == len(item["media"])
            and all(entry.get("type") == "photo" for entry in media)
        )
        result = {
            "status": "unknown",
            "verified": False,
            "external_reference": HOME["x"]
            + "/"
            + self.settings["identity"]
            + "/status/"
            + tweet_id,
            "target_verified": original,
        }
        checkpoint(result)
        self.step("receipt_saved", True)
        if self.response_failure:
            raise self.response_failure
        self.check_state()
        return self.verify({**payload, "receipt": result})

    def verify(self, payload):
        item = payload["payload"]
        result = payload.get("receipt") or payload.get("result", {})
        published = None
        url = result.get("external_reference") or payload.get("external_reference")
        if not url:
            return {"status": "unknown", "verified": False, "reason": "no_durable_receipt"}
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "www.reddit.com",
            "reddit.com",
            "x.com",
            "www.x.com",
        }:
            raise BackendError("InvalidReceiptURL")
        if self.platform == "reddit":
            if parsed.hostname not in {"www.reddit.com", "reddit.com"}:
                raise BackendError("InvalidReceiptURL")
            response = self.context.request.get(url.rstrip("/") + ".json")
            check_response(response)
            if not response.ok:
                return {**result, "status": "unknown", "verified": False}
            parts = parsed.path.strip("/").split("/")
            identifier = parts[5] if len(parts) > 5 else parts[3]
            observed = reddit_record(response.json(), identifier)
            published = observed_timestamp(observed.get("created_utc")) if observed else None
            verified = bool(
                observed
                and observed.get("author", "").casefold() == self.settings["identity"]
                and observed.get("selftext" if item["action"] == "post" else "body") == item["text"]
                and (item["action"] != "post" or observed.get("title") == item["title"])
            )
            if observed and item["action"] == "post":
                verified &= (
                    observed.get("subreddit", "").casefold()
                    == item["target"].removeprefix("r/").casefold()
                )
            elif observed:
                target = urlparse(item["target"]).path.strip("/").split("/")
                parent = "t1_" + target[5] if len(target) > 5 else "t3_" + target[3]
                verified &= observed.get("parent_id") == parent
        else:
            if parsed.hostname not in {"x.com", "www.x.com"}:
                raise BackendError("InvalidReceiptURL")
            self.step("receipt_navigation")
            self.navigate_x(url)
            self.step("receipt_lookup")
            article = self.tweet(url)
            self.step("receipt_validation")
            if item["action"] in {"like", "repost"}:
                verified = (
                    article.get_by_test_id(
                        "unlike" if item["action"] == "like" else "unretweet"
                    ).count()
                    == 1
                )
            else:
                authors = set(
                    re.findall(
                        r"@([A-Za-z0-9_]{1,15})\b",
                        article.get_by_test_id("User-Name").first.inner_text(),
                    )
                )
                verified = bool(
                    {author.casefold() for author in authors} == {self.settings["identity"]}
                    and result.get("target_verified")
                    and (
                        article.get_by_test_id("tweetText").inner_text()
                        if article.get_by_test_id("tweetText").count()
                        else ""
                    )
                    == item["text"]
                    and (
                        not item["media"]
                        or article.locator(
                            '[data-testid="tweetPhoto"], [data-testid="videoPlayer"]'
                        ).count()
                        > 0
                    )
                )
                timestamp = article.locator("time")
                if verified and timestamp.count() == 1:
                    published = observed_timestamp(timestamp.get_attribute("datetime"))
                if item["action"] == "post":
                    verified &= bool(published) and article.locator(
                        '[data-testid="tweetPhoto"]'
                    ).count() >= len(item["media"])
        return {
            **result,
            "status": "confirmed" if verified else "unknown",
            "verified": verified,
            "external_reference": url,
            "published_at": published if verified else None,
        }


def call(platform, label, operation, payload, checkpoint, authorize):
    settings = read_profile(platform, label)
    if platform == "x" and settings.get("connection_type") == "x_api":
        from .x_api import call as api_call

        return api_call(label, operation, payload, checkpoint, authorize)
    if settings.get("browser_channel") == "browser_use":
        from .browser_use import call as browser_use_call

        return browser_use_call(platform, settings, operation)
    if settings.get("browser_channel") == "tabbit":
        from .tabbit import call as tabbit_call

        return tabbit_call(platform, settings, operation, payload, checkpoint, authorize)

    from playwright.sync_api import sync_playwright

    progress = getattr(checkpoint, "progress", None)
    if operation == "execute" and progress:
        progress("browser_start", False)
    with sync_playwright() as runtime:
        context = launch_context(
            runtime,
            profile_path(platform, label),
            browser_channel=settings.get("browser_channel", "chromium"),
            headless=settings.get("browser_headless", True) is not False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            return BrowserBackend(platform, label, context, page, settings).run(
                operation, payload, checkpoint, authorize
            )
        finally:
            context.close()

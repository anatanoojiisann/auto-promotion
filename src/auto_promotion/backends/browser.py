"""Local, isolated browser profiles for Reddit and X. Never attaches to another PC."""

from __future__ import annotations

import re
import time
from urllib.parse import quote, urlparse

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


def identity(platform, context, page):
    if platform == "reddit":
        response = context.request.get(HOME[platform] + "/api/me.json")
        check_response(response)
        if not response.ok:
            raise BackendError("RedditIdentityUnavailable")
        return str(response.json().get("data", {}).get("name", "")).casefold()
    switcher = page.get_by_test_id("SideNav_AccountSwitcher_Button")
    switcher.wait_for(timeout=15000)
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
        observed = identity(self.platform, self.context, self.page)
        if not observed or observed != self.settings["identity"]:
            raise BackendError("BrowserIdentityMismatch")

    def run(self, operation, payload, checkpoint, authorize):
        self.progress = getattr(checkpoint, "progress", None) if operation == "execute" else None
        self.submission_started = bool(
            (payload.get("receipt") or payload.get("result") or {}).get("submission_started")
        )
        self.step("browser_ready")
        try:
            result = self._run(operation, payload, checkpoint, authorize)
            if operation in {"execute", "verify"}:
                if result.get("verified") is True:
                    self.step("completed")
                result.update(self.diagnostics())
            return result
        except Exception as exc:
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
        self.step("home_navigation")
        check_response(self.page.goto(HOME[self.platform], wait_until="domcontentloaded"))
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
            return self.discover(payload["policy"])
        if operation == "verify":
            return self.verify(payload)
        if operation != "execute":
            raise BackendError("UnsupportedBrowserOperation")
        return (self.reddit_execute if self.platform == "reddit" else self.x_execute)(
            payload, checkpoint, authorize
        )

    def discover(self, policy):
        records = {}
        maximum = policy["max_candidates"]
        for query in policy["queries"]:
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
                check_response(
                    self.page.goto(
                        HOME["x"] + "/search?q=" + quote(query) + "&src=typed_query&f=live"
                    )
                )
                self.page.get_by_test_id("tweet").first.wait_for(timeout=15000)
                articles = self.page.get_by_test_id("tweet")
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
        check_response(page.goto(item["target"], wait_until="domcontentloaded"))
        self.step("target_lookup")
        article = self.tweet(item["target"])
        self.step("identity_check")
        self.check_identity()
        action = item["action"]
        if action in {"like", "repost"}:
            done = "unlike" if action == "like" else "unretweet"
            if article.get_by_test_id(done).count() == 0:
                authorize()
                self.step("submit_click", True)
                visible_one(
                    article.get_by_test_id("like" if action == "like" else "retweet")
                ).click()
                if action == "repost":
                    authorize()
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
        authorize()
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/CreateTweet" in r.url, timeout=25000
        ) as accepted:
            self.step("submit_click", True)
            visible_one(submit_button).click()
            self.step("submission_response")
        self.step("parse_receipt")
        data = (
            check_response(accepted.value)
            .json()
            .get("data", {})
            .get("create_tweet", {})
            .get("tweet_results", {})
            .get("result", {})
        )
        data = data.get("tweet", data)
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
        return self.verify({**payload, "receipt": result})

    def x_original_post(self, payload, checkpoint, authorize):
        from playwright.sync_api import expect

        item, page = payload["payload"], self.page
        if not item.get("target_owned") or not owned_x_target(
            item["target"], self.settings["identity"]
        ):
            raise BackendError("XOwnedTargetRequired")
        self.step("post_composer", False)
        check_response(page.goto("https://x.com/compose/post", wait_until="domcontentloaded"))
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
        authorize()
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/CreateTweet" in r.url, timeout=25000
        ) as accepted:
            self.step("submit_click", True)
            visible_one(button).click()
        self.step("parse_receipt", True)
        data = (
            check_response(accepted.value)
            .json()
            .get("data", {})
            .get("create_tweet", {})
            .get("tweet_results", {})
            .get("result", {})
        )
        data = data.get("tweet", data)
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
            check_response(self.page.goto(url, wait_until="domcontentloaded"))
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
            headless=True,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            return BrowserBackend(platform, label, context, page, settings).run(
                operation, payload, checkpoint, authorize
            )
        finally:
            context.close()

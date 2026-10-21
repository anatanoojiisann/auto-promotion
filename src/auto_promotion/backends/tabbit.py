"""X session checks and account-opted-in replies through Tabbit's stable launcher."""

import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse

from .common import BackendError, observed_timestamp, owned_x_target


def launcher_path():
    if os.name == "nt":
        return Path.home() / "AppData/Local/Tabbit/LocalAgent/bin/tabbit-cli.exe"
    return Path.home() / ".local/bin/tabbit-cli"


def cli(arguments, instance, *, code=None):
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
                timeout=75,
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
    try:
        if inventory.get("truncated"):
            raise BackendError("TabbitInventoryTruncated")
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
                if urlparse(tab["url"]).path.strip("/").casefold() == expected
            ]
        selected = preferred[0] if len(preferred) == 1 else None
        if selected is None and len(available) == 1:
            selected = available[0]

        request_id = "identity-" + uuid.uuid4().hex
        arguments = ["nodejs", "--task", task, "--request-id", request_id, "--timeout-ms", "60000"]
        if selected:
            arguments.extend(["--tab", str(selected["tabId"])])
        # Account menu and Profile are observed controls on the authenticated X UI.
        code = r"""
if (!/^https:\/\/(www\.)?x\.com\//.test(page.url())) {
  await page.goto("https://x.com/home", {waitUntil: "domcontentloaded"});
}
const menu = page.getByRole("button", {name: "Account menu", exact: true});
await menu.waitFor({state: "visible", timeout: 15000});
const text = await menu.innerText();
const handles = [...new Set([...text.matchAll(/@([A-Za-z0-9_]{1,15})\b/g)]
  .map(m => m[1].toLowerCase()))];
const profile = await page.getByRole("link", {name: "Profile", exact: true})
  .getAttribute("href");
return {handles, profile};
"""
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
        handles = observed.get("handles", [])
        verified = (
            handles == [expected] and str(observed.get("profile", "")).casefold() == "/" + expected
        )
        return {
            "healthy": verified,
            "service_ready": True,
            "identity_verified": verified,
            "account_identity": expected if verified else None,
            "permissions": [],
            "reason": "tabbit_session_identity_verified" if verified else "TabbitIdentityMismatch",
        }
    finally:
        if settled:
            finished = cli(["finish", "--task", task], instance)
            if finished.get("forcedCleanup") or finished.get("ok") is False:
                raise BackendError("TabbitFinishIncomplete")


IDENTITY_JS = r"""
async function checkIdentity() {
  const menu = page.getByRole("button", {name: "Account menu", exact: true, includeHidden: true});
  await menu.waitFor({state: "visible", timeout: 15000});
  const handles = [...new Set([...(await menu.innerText()).matchAll(/@([A-Za-z0-9_]{1,15})\b/g)]
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

PREPARE_JS = r"""
await page.goto(item.action === "post" ? "https://x.com/compose/post" : item.target, {waitUntil: "domcontentloaded"});
await checkIdentity();
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
await editor.waitFor({state: "visible", timeout: 15000});
if (item.action === "post") assert((await editor.innerText()) === "" &&
  await scope.locator('[data-testid="attachments"] img, [data-testid="attachments"] video').count() === 0, "XComposerNotEmpty");
await editor.click();
await editor.press("Control+A");
await editor.press("Backspace");
await editor.pressSequentially(item.text);
await expect(editor).toHaveText(item.text);
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
await expect(editor).toHaveText(item.text);
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
const text = await article.getByTestId("tweetText").count() ? await article.getByTestId("tweetText").innerText() : "";
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

"""Fixed TikTok desktop operations on an existing, identity-bound browser.

No login, CAPTCHA interaction, private API calls, or automatic submission retry.
Tabbit owns the desktop session; native bridge owns the durable once-only claim.
"""

import json
import re
import uuid
from urllib.parse import parse_qs, urlparse

from ..models import digest
from .common import BackendError


def desktop_profile(settings):
    return settings.get("browser_channel") in {"tabbit", "chrome", "chromium", "msedge"}


def tabbit_candidate_enabled(settings):
    """Explicit experimental enrollment; existing desktop accounts keep their route."""
    instance = settings.get("tabbit_instance")
    return bool(
        settings.get("connection_type") == "tiktok_tabbit_candidate"
        and settings.get("experimental_tabbit_enabled") is True
        and settings.get("browser_channel") == "tabbit"
        and settings.get("instance_id") == settings.get("tabbit_instance")
        and isinstance(instance, str)
        and bool(re.fullmatch(r"[0-9A-F]{16}", instance))
    )


def video_target(url, video_id):
    parsed = urlparse(url)
    match = re.fullmatch(r"/@[A-Za-z0-9_.]+/(?:video|photo)/([1-9][0-9]{0,29})/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.tiktok.com", "tiktok.com"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.fragment
        or not match
        or match.group(1) != video_id
    ):
        raise BackendError("TikTokVideoTargetMismatch")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query.get("video_id", [video_id]) != [video_id] or len(query.get("comment_id", [])) > 1:
        raise BackendError("TikTokVideoTargetMismatch")
    return "https://www.tiktok.com" + parsed.path.rstrip("/")


def validate_item(item):
    if item.get("action") not in {"comment", "reply"}:
        raise BackendError("TikTokDesktopActionUnsupported")
    video_target(item["target"], item.get("tiktok_video_id", ""))
    parent = item.get("tiktok_comment_id", "")
    if item["action"] == "reply" and not re.fullmatch(r"[1-9][0-9]{0,29}", parent):
        raise BackendError("TikTokParentCommentRequired")
    if item["action"] == "comment" and parent:
        raise BackendError("TikTokTopLevelCommentCannotReply")
    bound = parse_qs(urlparse(item["target"]).query).get("comment_id")
    if bound and bound != [parent]:
        raise BackendError("TikTokParentCommentMismatch")
    if (
        not item.get("text", "").strip()
        or len(item["text"].encode("utf-8")) > 1200
        or item.get("media")
        or item.get("title")
        or item.get("tiktok_options")
        or item.get("recipient_id")
        or item.get("reply_to")
        or item.get("discord_reply_to")
    ):
        raise BackendError("TikTokDesktopCommentFieldsInvalid")


# Each phase runs against the same task/tab. It rechecks identity and native
# document binding; preparation never clicks Post. No coordinates or scrolling.
PROGRAM = r"""
const visible = async l => {
  const values=[];
  if(await l.count()>50)throw new Error('TikTokObservationLimit');
  for(let i=0;i<await l.count();i++) if(await l.nth(i).isVisible()&&await l.nth(i).evaluate(e=>{for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);if(Number(s.opacity)===0||n.getAttribute('aria-hidden')==='true')return false;}return true;})) values.push(l.nth(i));
  return values;
};
const one = async (l,reason) => {const a=await visible(l);if(a.length!==1) throw new Error(reason);return a[0];};
const targetID = url => {
  const u=new URL(url);const m=u.pathname.match(/^\/@[A-Za-z0-9_.]+\/(?:video|photo)\/([1-9][0-9]{0,29})\/?$/);
  return u.protocol==='https:'&&['www.tiktok.com','tiktok.com'].includes(u.hostname)&&m?m[1]:null;
};
const state = async () => {
  const u=new URL(page.url());
  if(u.protocol!=='https:'||!['www.tiktok.com','tiktok.com'].includes(u.hostname)) throw new Error('TikTokPageUnavailable');
  if(/^\/(?:login|signup|captcha|challenge|verify)(?:\/|$)/.test(u.pathname)) throw new Error('TikTokManualVerificationRequired');
  if((await visible(page.locator('iframe[src^="https://challenges.cloudflare.com/"], [data-e2e="captcha"], #captcha_container'))).length) throw new Error('TikTokManualVerificationRequired');
};
const identity = async () => {
  await state();
  const profile=await one(page.locator('a[data-e2e="nav-profile"], a[data-e2e="profile-icon"]'),'TikTokIdentityUnavailable');
  const href=await profile.getAttribute('href');
  const u=new URL(href,'https://www.tiktok.com');
  if(u.origin!=='https://www.tiktok.com'||u.pathname.toLowerCase()!=='/@'+expected) throw new Error('TikTokIdentityMismatch');
};
const documentBound = async () => {
  await identity();
  if(targetID(page.url())!==item.tiktok_video_id) throw new Error('TikTokVideoTargetMismatch');
  const canonical=await page.locator('link[rel="canonical"]').getAttribute('href');
  if(targetID(canonical)!==item.tiktok_video_id) throw new Error('TikTokVideoTargetMismatch');
  // Multiple visible videos/frames or modal editors are ambiguous, not permission to guess.
  if((await visible(page.locator('video'))).length!==1) throw new Error('TikTokVideoRegionAmbiguous');
  if((await visible(page.locator('[role="dialog"], iframe'))).length) throw new Error('TikTokCommentRegionAmbiguous');
};
const parentRow = async () => {
  let candidates=await visible(page.locator('[data-comment-id="'+item.tiktok_comment_id+'"]'));
  const bound=[];
  for(const r of candidates)if(await r.getAttribute('data-video-id')===item.tiktok_video_id)bound.push(r);
  candidates=bound;
  if(candidates.length===0){
    const links=page.locator('a[href*="comment_id="]');
    if(await links.count()>100) throw new Error('TikTokParentCommentUnavailable');
    for(let i=0;i<await links.count();i++){
      const l=links.nth(i);if(!await l.isVisible())continue;
      const u=new URL(await l.getAttribute('href'),'https://www.tiktok.com');
      if(targetID(u.href)!==item.tiktok_video_id||u.searchParams.getAll('comment_id').length!==1||u.searchParams.get('comment_id')!==item.tiktok_comment_id)continue;
      const p=l.locator('xpath=ancestor::*[@data-comment-id][1]');
      if(await p.count()===1&&await p.getAttribute('data-comment-id')===item.tiktok_comment_id&&(!await p.getAttribute('data-video-id')||await p.getAttribute('data-video-id')===item.tiktok_video_id))candidates.push(p);
    }
  }
  if(candidates.length!==1)throw new Error('TikTokParentCommentUnavailable');
  return candidates[0];
};
const editor = async () => one((item.action==='reply'?await parentRow():page).locator(item.action==='reply'?'[data-e2e="comment-reply-text"] [contenteditable="true"]':'[data-e2e="comment-text"] [contenteditable="true"]'),'TikTokCommentEditorAmbiguous');
const submitButton = async () => one((item.action==='reply'?await parentRow():page).locator(item.action==='reply'?'[data-e2e="comment-reply-post"]':'[data-e2e="comment-post"]'),'TikTokCommentSubmitUnavailable');
const navigate = async () => {
 let status=null,wait=900;
 const response=r=>{const q=r.request();const u=new URL(r.url());if(q.isNavigationRequest()&&q.frame()===page.mainFrame()&&['www.tiktok.com','tiktok.com'].includes(u.hostname)){status=r.status();const v=r.headers()['retry-after'];const n=/^[0-9]+$/.test(v||'')?Number(v):Math.ceil((Date.parse(v)-Date.now())/1000);if(Number.isFinite(n)&&n>0)wait=Math.max(wait,n);}};
 page.on('response',response);
 try{const r=await page.goto(target,{waitUntil:'domcontentloaded',timeout:20000});status=r?r.status():status;}
 catch(error){if(status!==403&&status!==429)throw error;}
 finally{page.removeListener('response',response);}
 if(status===403||status===429){const error=new Error(status===403?'TikTokAccessDenied':'TikTokRateLimited');error.retryAfter=wait;throw error;}
 if(status>=400)throw new Error('TikTokPageUnavailable');
};
let clicked=false;
try {
  if(phase==='health'){
    await identity();return {healthy:true,identity_verified:true,account_identity:identityKey,permissions:['comment','reply'],reason:'tiktok_desktop_identity_verified'};
  }
  if(phase==='prepare'||phase==='preflight'){
    await identity();
    const existing=await visible(page.locator('[data-e2e="comment-text"] [contenteditable="true"], [data-e2e="comment-reply-text"] [contenteditable="true"]'));
    for(const e of existing)if((await e.innerText()).trim())throw new Error('TikTokCommentDraftNotEmpty');
    await navigate();
    await documentBound();
    if(item.action==='reply')await (await one((await parentRow()).locator('[data-e2e="comment-reply-1"]'),'TikTokParentReplyUnavailable')).click();
    const e=await editor();
    if((await e.innerText()).trim())throw new Error('TikTokCommentDraftNotEmpty');
    if(phase==='preflight'){await submitButton();return {prepared:true,video_id:item.tiktok_video_id,parent_comment_id:item.tiktok_comment_id||'',text_hash:textHash};}
    await e.fill(item.text);
    if(await e.innerText()!==item.text)throw new Error('TikTokCommentTextMismatch');
    await documentBound();await submitButton();
    return {prepared:true,video_id:item.tiktok_video_id,parent_comment_id:item.tiktok_comment_id||'',text_hash:textHash};
  }
  if(phase==='submit'){
    await documentBound();
    if(item.action==='reply')await parentRow();
    if(await (await editor()).innerText()!==item.text)throw new Error('TikTokCommentTextMismatch');
    const button=await submitButton();if(!await button.isEnabled())throw new Error('TikTokCommentSubmitUnavailable');
    // Observe the normal UI request's response; never issue a private API call.
    const requests=[];
    const observe=q=>{const u=new URL(q.url());if(clicked&&q.method()==='POST'&&q.frame()===page.mainFrame()&&u.protocol==='https:'&&['www.tiktok.com','tiktok.com'].includes(u.hostname)&&u.pathname==='/api/comment/publish/')requests.push(q);};
    page.on('request',observe);
    let response=null;
    try{
      const pending=page.waitForResponse(r=>requests.length===1&&r.request()===requests[0],{timeout:5000}).catch(()=>null);
      clicked=true;
      await button.click();
      response=await pending;
    }finally{page.removeListener('request',observe);}
    let id=null;
    if(requests.length===1&&response&&response.status()===200){
      const d=await response.json();const c=d&&d.comment;
      const parent=c&&String(c.reply_id||'');
      if(c&&String(c.aweme_id)===item.tiktok_video_id&&c.text===item.text&&c.user&&String(c.user.unique_id).toLowerCase()===expected&&(parent==='0'?'':parent)===(item.action==='reply'?item.tiktok_comment_id:'')) id=String(c.cid||'');
    }
    return {status:'unknown',verified:false,submission_started:true,comment_id:id&&/^[1-9][0-9]{0,29}$/.test(id)?id:null,reason:'TikTokIndependentReceiptRequired'};
  }
  if(phase==='verify'){
    if(!receipt.comment_id||!/^[1-9][0-9]{0,29}$/.test(receipt.comment_id))return {status:'unknown',verified:false,reason:'TikTokIndependentReceiptRequired'};
    await navigate();
    await documentBound();
    // An ID from a submit-side toast is insufficient. Only a separate native
    // read with exact owner, post, parent, body and link may confirm delivery.
    const rows=await visible(page.locator('[data-comment-id]'));
    const matches=[];
    for(const r of rows){
      if(await r.getAttribute('data-video-id')!==item.tiktok_video_id)continue;
      if((await r.getAttribute('data-author')||'').toLowerCase()!==expected)continue;
      const parent=await r.getAttribute('data-parent-comment-id')||'';
      if((parent==='0'?'':parent)!==(item.action==='reply'?item.tiktok_comment_id:''))continue;
      const texts=r.locator('[data-e2e="comment-level-1"], [data-e2e="comment-level-2"]');
      if(await texts.count()!==1||await texts.innerText()!==item.text)continue;
      const id=await r.getAttribute('data-comment-id');if(id!==receipt.comment_id)continue;
      const links=r.locator('a[href*="comment_id="]');if(await links.count()!==1)continue;
      const u=new URL(await links.getAttribute('href'),'https://www.tiktok.com');
      if(targetID(u.href)!==item.tiktok_video_id||u.searchParams.getAll('comment_id').length!==1||u.searchParams.get('comment_id')!==id)continue;
      matches.push({id,url:target+'?comment_id='+id});
    }
    if(matches.length!==1)return {status:'unknown',verified:false,reason:'TikTokIndependentReceiptRequired'};
    return {status:'confirmed',verified:true,comment_id:matches[0].id,external_reference:matches[0].url};
  }
  throw new Error('TikTokDesktopOperationUnsupported');
} catch(error) {
  const codes=['TikTokPageUnavailable','TikTokManualVerificationRequired','TikTokIdentityUnavailable','TikTokIdentityMismatch','TikTokVideoTargetMismatch','TikTokVideoRegionAmbiguous','TikTokCommentRegionAmbiguous','TikTokParentCommentUnavailable','TikTokParentReplyUnavailable','TikTokCommentEditorAmbiguous','TikTokCommentSubmitUnavailable','TikTokCommentDraftNotEmpty','TikTokCommentTextMismatch'];
  codes.push('TikTokAccessDenied','TikTokRateLimited','TikTokObservationLimit');
  return {blocked:true,submission_started:clicked,reason:codes.includes(error.message)?error.message:'TikTokDesktopObservationUnavailable',...(error.message==='TikTokAccessDenied'?{failure_class:'access_denied',retry_after_seconds:error.retryAfter||900}:{}),...(error.message==='TikTokRateLimited'?{rate_limited:true,retry_after_seconds:error.retryAfter||900}:{})};
}
"""


def call(settings, operation, payload, checkpoint, authorize):
    from .tabbit import cli

    if not tabbit_candidate_enabled(settings):
        prior = payload.get("receipt") or payload.get("result") or {}
        return {
            **prior,
            "status": "failed_known" if operation == "execute" and not prior else "unknown",
            "verified": False,
            "healthy": False,
            "allowed": False,
            "identity_verified": False,
            "reason": "TikTokTabbitCandidateDisabled",
        }
    if settings.get("browser_channel") != "tabbit":
        raise BackendError("TikTokExistingTabbitConnectionRequired")
    expected = (
        str(settings.get("username") or settings.get("identity", "")).removeprefix("@").casefold()
    )
    instance = settings.get("tabbit_instance", "")
    if not re.fullmatch(r"[a-z0-9_.]{1,24}", expected) or not re.fullmatch(
        r"[0-9A-F]{16}", instance
    ):
        raise BackendError("TikTokDesktopBindingUnavailable")
    if operation not in {"health", "preflight", "execute", "verify"}:
        return {"healthy": False, "allowed": False, "reason": "TikTokDesktopReadCapabilityRequired"}
    item = payload.get("payload", {})
    if operation != "health":
        validate_item(item)
    binding = digest(
        [
            settings["identity"],
            payload.get("task_id"),
            payload.get("item_id"),
            payload.get("attempt_id"),
            item,
        ]
    )
    prior = payload.get("receipt") or payload.get("result") or {}
    if operation == "verify" and prior.get("tiktok_receipt_binding") != binding:
        return {
            **prior,
            "status": "unknown",
            "verified": False,
            "reason": "TikTokReceiptBindingMismatch",
        }
    task = "TikTok " + operation + " " + str(payload.get("item_id") or uuid.uuid4().hex)
    inventory = cli(["create", "--task", task, "--limit", "200"], instance)
    if inventory.get("task", {}).get("reused") or inventory.get("truncated"):
        raise BackendError("TikTokDesktopRequestPending")
    tabs = [
        t
        for t in inventory.get("tabs", [])
        if t.get("state") == "available"
        and urlparse(t.get("url", "")).hostname in {"www.tiktok.com", "tiktok.com"}
    ]
    preferred = [t for t in tabs if t.get("tabId") == settings.get("tabbit_tab_id")]
    selected = preferred[0] if len(preferred) == 1 else tabs[0] if len(tabs) == 1 else None
    settled = True
    if selected is None:
        cli(["finish", "--task", task], instance)
        raise BackendError("TikTokDesktopTabAmbiguous")

    receipt = dict(prior)

    def program(phase):
        nonlocal settled
        request = phase + "-" + uuid.uuid4().hex
        variables = {
            "phase": phase,
            "expected": expected,
            "identityKey": settings["identity"],
            "item": item,
            "target": video_target(item["target"], item["tiktok_video_id"]) if item else "",
            "textHash": digest(item.get("text", "")),
            "receipt": receipt,
        }
        code = (
            "\n".join("const " + k + "=" + json.dumps(v) + ";" for k, v in variables.items())
            + "\n"
            + PROGRAM
        )
        settled = False
        answer = cli(
            [
                "nodejs",
                "--task",
                task,
                "--tab",
                str(selected["tabId"]),
                "--request-id",
                request,
                "--timeout-ms",
                "45000",
            ],
            instance,
            code=code,
        )
        if answer.get("status") in {"queued", "running"}:
            answer = cli(
                ["receipt", "--task", task, "--request-id", request, "--wait-ms", "45000"], instance
            )
        settled = answer.get("status") in {"succeeded", "failed", "interrupted"}
        if settled and any(
            isinstance(issue, dict)
            and issue.get("code") == "SITE_ACCESS_BLOCKED"
            and issue.get("operation") == "navigation"
            and issue.get("origin") in {"https://www.tiktok.com", "https://tiktok.com"}
            and issue.get("details", {}).get("status") == 403
            for issue in answer.get("result", {}).get("issuesDelta", [])
        ):
            observed_wait = answer.get("result", {}).get("value", {}).get("retry_after_seconds")
            return {
                "blocked": True,
                "reason": "TikTokAccessDenied",
                "failure_class": "access_denied",
                "retry_after_seconds": max(900, observed_wait)
                if type(observed_wait) in {int, float}
                else 900,
                "submission_started": phase == "submit",
            }
        if not settled or answer.get("status") != "succeeded":
            raise BackendError("TikTokDesktopRequestPending")
        return answer.get("result", {}).get("value", {})

    submitted = False
    try:
        if operation == "health":
            return program("health")
        if operation in {"preflight", "execute"}:
            prepared = program("preflight" if operation == "preflight" else "prepare")
            if (
                not prepared.get("prepared")
                or prepared.get("video_id") != item["tiktok_video_id"]
                or prepared.get("parent_comment_id") != item.get("tiktok_comment_id", "")
                or prepared.get("text_hash") != digest(item["text"])
            ):
                return {
                    **prepared,
                    "status": "failed_known",
                    "confirmed_not_submitted": True,
                    "allowed": False,
                    "verified": False,
                    "execution_step": "tiktok_video_prepare",
                }
            if operation == "preflight":
                return {"allowed": True, "target_verified": True}
            authorize()
            progress = getattr(checkpoint, "progress", lambda *_: None)
            progress("tiktok_comment_submit", True)
            submitted = True
            result = program("submit")
            if result.get("blocked") and result.get("submission_started") is False:
                return {
                    **result,
                    "status": "failed_known",
                    "verified": False,
                    "confirmed_not_submitted": True,
                }
            result.update(
                status="unknown",
                verified=False,
                submission_started=True,
                tiktok_receipt_binding=binding,
            )
            receipt.update(result)
            checkpoint(result)
        result = program("verify")
        result = {**receipt, **result}
        result.update(tiktok_receipt_binding=binding)
        return result
    except BackendError as exc:
        return {
            **receipt,
            "status": "unknown" if submitted or operation == "verify" else "failed_known",
            "verified": False,
            "confirmed_not_submitted": not submitted and operation != "verify",
            "reason": exc.safe_code,
            "submission_started": submitted or bool(prior.get("submission_started")),
            "tiktok_receipt_binding": binding,
        }
    finally:
        if settled:
            # A cleanup failure must not replace an accepted/unknown outcome.
            try:
                cli(["finish", "--task", task], instance)
            except BackendError:
                pass

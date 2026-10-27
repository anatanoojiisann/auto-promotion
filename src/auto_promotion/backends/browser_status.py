"""Detect visible platform verification/restriction notices without interacting."""

import re
from urllib.parse import urlparse

from .common import BackendError

# Only platform notices and visible challenge controls are inspected. User posts
# mentioning verification or suspension must not be treated as account state.
STATUS_JS = r"""() => {
  const visible = el => {
    if (![...el.getClientRects()].some(r => r.width > 0 && r.height > 0)) return false;
    for (let node = el; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.visibility === 'hidden' || style.visibility === 'collapse' ||
          Number(style.opacity) === 0 || node.getAttribute('aria-hidden') === 'true') return false;
    }
    return true;
  };
  const path = location.pathname;
  if (/^\/account\/suspended\/?$/.test(path)) return 'BrowserAccountRestricted';
  if (/^\/account\/access(?:\/|$)/.test(path)) return 'BrowserVerificationRequired';
  const challenges = [...document.querySelectorAll(
    'iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="arkoselabs"],'+
    'iframe[src*="challenges.cloudflare.com"],iframe[title*="challenge" i],'+
    '[data-testid="ocfEnterTextTextInput"],input[name="arkose"],.g-recaptcha,.h-captcha'
  )];
  if (challenges.some(visible)) return 'BrowserVerificationRequired';
  const notices = [...document.querySelectorAll('h1,h2,[role="heading"],[role="alert"],[role="dialog"]')]
    .filter(el => visible(el) && !el.closest('article,[data-testid="tweet"],shreddit-post'))
    .map(el => el.innerText || '').join('\n');
  if (/your account (?:is|has been) (?:locked|suspended)|account (?:is |has been )?(?:temporarily )?(?:restricted|limited)|账号已被(?:锁定|冻结|限制)|帐[户号]已被(?:锁定|冻结|限制)/i.test(notices))
    return 'BrowserAccountRestricted';
  if (/verify (?:that )?you are (?:a )?human|authenticate your account|help us keep your account safe|人机验证|验证您是人类|证明您是真人|请验证您是真人|正在进行安全验证|完成安全验证/i.test(notices))
    return 'BrowserVerificationRequired';
  return null;
}"""


def check_status(page):
    path = urlparse(page.url).path
    if re.match(r"^/account/access(?:/|$)", path):
        raise BackendError("BrowserVerificationRequired")
    if re.fullmatch(r"/account/suspended/?", path):
        raise BackendError("BrowserAccountRestricted")
    reason = page.evaluate(STATUS_JS)
    if reason in {"BrowserVerificationRequired", "BrowserAccountRestricted"}:
        raise BackendError(reason)


def check_context_status(context):
    """Inspect existing X pages before a health probe can replace them with /home."""
    for page in getattr(context, "pages", []):
        if page.is_closed():
            continue
        if urlparse(page.url).hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            check_status(page)

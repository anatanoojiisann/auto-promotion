/* Desktop executor helper. Load in the existing browser task, never in a background sender.
 * Page selectors below were observed on TikTok on 2026-10-26. No private API or app state.
 */
"use strict";

var tiktokDesktop =
  globalThis.__autoPromotionTikTokDesktop ||
  (globalThis.__autoPromotionTikTokDesktop = (() => {
    const consumed = new Set();
    const preparedParents = new Map();

    function readDOM() {
      const visible = (el) =>
        !!el &&
        el.getBoundingClientRect().width > 0 &&
        el.getBoundingClientRect().height > 0 &&
        getComputedStyle(el).visibility !== "hidden";
      const one = (selector) => {
        const found = [...document.querySelectorAll(selector)].filter(visible);
        return found.length === 1 ? found[0] : null;
      };
      const current = new URL(location.href);
      const siteVerified =
        current.protocol === "https:" &&
        ["www.tiktok.com", "tiktok.com"].includes(current.host);
      const match = /^\/@([^/]+)\/video\/([0-9]{1,30})\/?$/.exec(
        current.pathname,
      );
      const profiles = [...document.querySelectorAll('[aria-label="Profile"]')]
        .filter(visible)
        .map((el) => el.closest("a")?.getAttribute("href"))
        .filter(Boolean);
      const identities = [
        ...new Set(
          profiles
            .map((href) =>
              /^\/@([A-Za-z0-9_.]+)\/?$/.exec(href)?.[1]?.toLowerCase(),
            )
            .filter(Boolean),
        ),
      ];
      const editor = one(
        '[data-e2e="comment-input"] [role="textbox"][contenteditable="true"]',
      );
      const post = one('[data-e2e="comment-post"]');
      const editorText = (el) => {
        if (!el) return "";
        const blocks = [...el.querySelectorAll('[data-block="true"]')];
        return blocks.length
          ? blocks.map((b) => b.textContent || "").join("\n")
          : el.textContent || "";
      };
      const comments = [
        ...document.querySelectorAll('[data-e2e="comment-level-1"]'),
      ]
        .filter(visible)
        .slice(0, 100)
        .map((el) => {
          const content = el.parentElement;
          const authorLink = content.querySelector(
            '[data-e2e="comment-username-1"] a',
          );
          const link = [...content.querySelectorAll("a")].find((a) =>
            /[?&]comment_id=[0-9]+/.test(a.getAttribute("href") || ""),
          );
          const id = link
            ? new URL(
                link.getAttribute("href"),
                location.href,
              ).searchParams.get("comment_id")
            : null;
          let scope = content,
            steps = 0;
          while (
            scope &&
            steps < 6 &&
            !scope.querySelector('[data-e2e="comment-reply-input"]')
          ) {
            scope = scope.parentElement;
            steps++;
          }
          const unique =
            scope &&
            scope.querySelectorAll('[data-e2e="comment-level-1"]').length === 1;
          const reply = unique
            ? scope.querySelector(
                '[data-e2e="comment-reply-input"] [role="textbox"][contenteditable="true"]',
              )
            : null;
          const replyPost = unique
            ? scope.querySelector('[data-e2e="comment-reply-post"]')
            : null;
          return {
            text: el.innerText,
            author_profile: authorLink?.getAttribute("href") || null,
            comment_id: id,
            id_verified: !!id,
            reply_editor_available: visible(reply),
            reply_editor_text: editorText(reply),
            reply_post_enabled:
              visible(replyPost) &&
              !replyPost.disabled &&
              replyPost.getAttribute("aria-disabled") !== "true",
          };
        });
      // Missing IDs and incomplete pagination remain explicit, never inferred from order.
      return {
        url: current.href,
        site_verified: siteVerified,
        video_id: match?.[2] || null,
        identity: identities.length === 1 ? identities[0] : null,
        authenticated_navigation_verified: identities.length === 1,
        editor_available: !!editor,
        editor_text: editorText(editor),
        post_available: !!post,
        post_enabled:
          !!post &&
          !post.disabled &&
          post.getAttribute("aria-disabled") !== "true",
        comments,
        coverage: "partial",
        observed_at: Date.now() / 1000,
      };
    }

    const read = (page) => page.evaluate(readDOM);

    function check(snapshot, item, connection) {
      const p = item.payload;
      if (
        !snapshot.site_verified ||
        !snapshot.authenticated_navigation_verified ||
        snapshot.identity !== connection.identity
      )
        throw new Error("TikTokBrowserIdentityMismatch");
      if (!snapshot.video_id || snapshot.video_id !== p.tiktok_video_id)
        throw new Error("TikTokBrowserWrongVideo");
      if (p.action === "comment") {
        if (
          !snapshot.editor_available ||
          snapshot.editor_text !== p.text ||
          !snapshot.post_enabled
        )
          throw new Error("TikTokBrowserEditorNotReady");
      } else if (p.action === "reply") {
        const parent = preparedParents.get(item.item_id);
        const matches = (snapshot.comments || []).filter(
          (c) =>
            c.text === parent?.text &&
            c.author_profile === parent?.author_profile,
        );
        if (
          !parent ||
          parent.comment_id !== p.tiktok_comment_id ||
          matches.length !== 1 ||
          !matches[0].reply_editor_available ||
          matches[0].reply_editor_text !== p.text ||
          !matches[0].reply_post_enabled
        )
          throw new Error("TikTokBrowserReplyNeedsIndependentParentProof");
      } else throw new Error("TikTokBrowserUnsupportedAction");
    }

    async function prepareComment(page, item, connection) {
      const p = item.payload;
      if (
        p.action !== "comment" ||
        p.media?.length ||
        p.title ||
        !p.text?.trim() ||
        p.text.length > 150
      )
        throw new Error("TikTokBrowserUnsupportedDraft");
      const snapshot = await read(page);
      if (
        !snapshot.site_verified ||
        !snapshot.authenticated_navigation_verified ||
        snapshot.identity !== connection.identity ||
        snapshot.video_id !== p.tiktok_video_id
      )
        throw new Error("TikTokBrowserContextMismatch");
      const editor = page.locator(
        '[data-e2e="comment-input"] [role="textbox"][contenteditable="true"]',
      );
      if ((await editor.count()) !== 1)
        throw new Error("TikTokBrowserEditorAmbiguous");
      // Never overwrite an unrelated draft.
      if (snapshot.editor_text && snapshot.editor_text !== p.text)
        throw new Error("TikTokBrowserExistingDraft");
      await editor.fill(p.text);
      const ready = await read(page);
      check(ready, item, connection);
      return ready; // Filling is local preparation; it never clicks Post.
    }

    async function submitComment(page, item, connection, permit) {
      const p = item.payload;
      if (
        !permit?.allowed ||
        !permit.submit_once ||
        permit.platform !== "tiktok" ||
        permit.item_id !== item.item_id ||
        permit.payload_hash !== item.payload_hash ||
        permit.target !== p.target ||
        permit.identity !== connection.identity ||
        permit.browser !== connection.browser ||
        permit.instance_id !== (connection.instance_id || "") ||
        !Number.isFinite(permit.expires_at) ||
        permit.expires_at <= Date.now() / 1000 ||
        consumed.has(item.item_id)
      )
        throw new Error("TikTokBrowserInvalidOrUsedPermit");
      const snapshot = await read(page);
      check(snapshot, item, connection);
      const post = page.locator(
        p.action === "reply"
          ? '[data-e2e="comment-reply-post"]'
          : '[data-e2e="comment-post"]',
      );
      if ((await post.count()) !== 1)
        throw new Error("TikTokBrowserSubmitAmbiguous");
      if (consumed.has(item.item_id) || permit.expires_at <= Date.now() / 1000)
        throw new Error("TikTokBrowserInvalidOrUsedPermit");
      consumed.add(item.item_id); // A click timeout is uncertain and can never replay.
      await post.click({ timeout: 15000 });
      return {
        status: "unknown",
        verified: false,
        submission_started: true,
        reason: "TikTokBrowserIndependentReceiptRequired",
      };
    }

    async function prepareReply(page, item, connection, parent) {
      const p = item.payload;
      if (
        p.action !== "reply" ||
        !p.text?.trim() ||
        p.text.length > 150 ||
        p.media?.length ||
        p.title ||
        !parent?.id_verified ||
        !parent.independent_read ||
        !parent.evidence ||
        parent.comment_id !== p.tiktok_comment_id ||
        parent.target !== p.target ||
        !parent.text ||
        !parent.author_profile ||
        !Number.isFinite(parent.observed_at) ||
        Math.abs(Date.now() / 1000 - parent.observed_at) > 300
      )
        throw new Error("TikTokBrowserParentEvidenceRequired");
      const before = await read(page);
      if (
        !before.site_verified ||
        !before.authenticated_navigation_verified ||
        before.identity !== connection.identity ||
        before.video_id !== p.tiktok_video_id
      )
        throw new Error("TikTokBrowserContextMismatch");
      const nodes = page.locator('[data-e2e="comment-level-1"]');
      const matches = await nodes.evaluateAll(
        (els, proof) =>
          els.flatMap((el, i) =>
            el.innerText === proof.text &&
            el.parentElement
              .querySelector('[data-e2e="comment-username-1"] a')
              ?.getAttribute("href") === proof.author_profile
              ? [i]
              : [],
          ),
        parent,
      );
      if (matches.length !== 1) throw new Error("TikTokBrowserParentAmbiguous");
      const currentReplies = (before.comments || []).filter(
        (c) => c.reply_editor_available,
      );
      if (currentReplies.some((c) => c.reply_editor_text))
        throw new Error("TikTokBrowserExistingDraft");
      await nodes
        .nth(matches[0])
        .locator("..")
        .locator('[data-e2e="comment-reply-1"]')
        .click();
      const editor = page.locator(
        '[data-e2e="comment-reply-input"] [role="textbox"][contenteditable="true"]',
      );
      await editor.waitFor({ state: "visible", timeout: 15000 });
      if ((await editor.count()) !== 1)
        throw new Error("TikTokBrowserEditorAmbiguous");
      await editor.fill(p.text);
      preparedParents.set(item.item_id, { ...parent });
      const ready = await read(page);
      check(ready, item, connection);
      return ready;
    }

    return {
      readDOM,
      read,
      prepareComment,
      prepareReply,
      submitComment,
      submit: submitComment,
    };
  })());

if (typeof module !== "undefined") module.exports = tiktokDesktop;

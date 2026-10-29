/* TikTok DM DOM adapter, including the observed Business Suite /messages iframe.
 * Private content stays in the explicit collection response, never notification text.
 */
"use strict";
var tiktokMessages =
  globalThis.__autoPromotionTikTokMessagesV1 ||
  (globalThis.__autoPromotionTikTokMessagesV1 = (() => {
    const consumed = new Set(),
      prepared = new Map();
    const trusted = (url) => {
      try {
        const u = new URL(url);
        return (
          u.protocol === "https:" &&
          ["www.tiktok.com", "tiktok.com"].includes(u.host)
        );
      } catch (_) {
        return false;
      }
    };
    function frameFor(page) {
      const frames = page
        .frames()
        .filter(
          (f) => trusted(f.url()) && new URL(f.url()).pathname === "/messages",
        );
      if (frames.length !== 1)
        throw new Error("TikTokDMFrameUnavailableOrAmbiguous");
      if (!trusted(page.url())) throw new Error("TikTokDMUntrustedPage");
      return frames[0];
    }
    function readDOM(identity) {
      const visible = (e) =>
        !!e &&
        e.getBoundingClientRect().width > 0 &&
        e.getBoundingClientRect().height > 0;
      const selected = [
        ...document.querySelectorAll(
          '[data-e2e="dm-new-conversation-item"][aria-selected="true"]',
        ),
      ];
      const box = document.querySelector('[data-e2e="dm-new-chatbox"]');
      const handle = (
        box?.querySelector('[data-e2e="chat-uniqueid"]')?.textContent || ""
      )
        .trim()
        .replace(/^@/, "")
        .toLowerCase();
      const peerVerified =
        !!handle &&
        [...box.querySelectorAll("a")].some(
          (a) => a.getAttribute("href") === "/@" + handle,
        );
      const conversationId =
        selected.length === 1 ? selected[0].getAttribute("data-conv-id") : null;
      const messages = [
        ...document.querySelectorAll('[data-e2e="dm-new-chat-item"]'),
      ]
        .slice(-200)
        .map((el) => {
          const native =
            el
              .querySelector('[data-e2e="dm-new-more-btn"]')
              ?.id.match(/^more-action-icon-msg-([0-9]{1,30})$/)?.[1] || "";
          const sender =
            (el.querySelector("a")?.getAttribute("href") || "")
              .match(/^\/@([A-Za-z0-9_.]+)\/?$/)?.[1]
              ?.toLowerCase() || "";
          let sibling = el.parentElement?.previousElementSibling,
            label = "",
            steps = 0;
          while (sibling && steps++ < 12) {
            const time = sibling.matches('[data-e2e="dm-new-time-separator"]')
              ? sibling
              : sibling.querySelector('[data-e2e="dm-new-time-separator"]');
            if (time) {
              label = time.textContent.trim();
              break;
            }
            sibling = sibling.previousElementSibling;
          }
          const at = /^[A-Z][a-z]+ \d{1,2}, \d{4} \d{2}:\d{2}$/.test(label)
            ? Date.parse(label)
            : NaN;
          return {
            message_id: native,
            id_verified: !!native,
            direction:
              sender === identity
                ? "outbound"
                : sender === handle
                  ? "inbound"
                  : "unknown",
            sender_username: sender,
            text:
              el.querySelector('[data-e2e="dm-new-message-text"]')?.innerText ||
              "",
            occurred_at: Number.isFinite(at) ? at / 1000 : null,
            timestamp_verified: Number.isFinite(at),
            timestamp_label: label,
          };
        });
      const editors = [
        ...document.querySelectorAll(
          '[data-e2e="dm-new-input-editor"] [role="textbox"][contenteditable="true"]',
        ),
      ].filter(visible);
      const editor = editors.length === 1 ? editors[0] : null;
      const blocks = editor
        ? [...editor.querySelectorAll('[data-block="true"]')]
        : [];
      const editorText = blocks.length
        ? blocks.map((b) => b.textContent || "").join("\n")
        : editor?.textContent || "";
      // Discover a visible send control after the exact authorized draft is filled.
      const area = document.querySelector('[data-e2e="message-input-area"]');
      const send = [
        ...(area?.querySelectorAll('[data-e2e],button,[role="button"]') || []),
      ].filter(
        (el) =>
          visible(el) &&
          (/send/i.test(el.getAttribute("data-e2e") || "") ||
            /^(Send|发送)$/i.test(el.getAttribute("aria-label") || "")),
      );
      const control = send.length === 1 ? send[0] : null;
      const e2e = control?.getAttribute("data-e2e"),
        label = control?.getAttribute("aria-label");
      const sendControl =
        e2e && /^[A-Za-z0-9_-]+$/.test(e2e)
          ? { e2e }
          : label
            ? { label }
            : null;
      return {
        source_url: location.href,
        inbox_verified: !!document.querySelector(
          '[data-e2e="dm-new-conversation-list"]',
        ),
        observed_at: Date.now() / 1000,
        coverage: "partial",
        recipient_id: handle,
        recipient_verified: peerVerified,
        conversation_id: conversationId,
        conversation_verified: !!conversationId && peerVerified,
        conversations:
          conversationId && peerVerified
            ? [
                {
                  conversation_id: conversationId,
                  peer_username: handle,
                  peer_verified: true,
                  source_url: location.href,
                  messages,
                },
              ]
            : [],
        editor_available: !!editor,
        editor_text: editorText,
        can_message:
          !!editor &&
          !!control &&
          !control.disabled &&
          control.getAttribute("aria-disabled") !== "true",
        send_control: sendControl,
        blocked_reason: (!editor && box?.innerText) || null,
        available_conversations: [
          ...document.querySelectorAll('[data-e2e="dm-new-conversation-item"]'),
        ]
          .slice(0, 100)
          .map((el) => ({
            conversation_id: el.getAttribute("data-conv-id"),
            nickname:
              el.querySelector('[data-e2e="dm-new-conversation-nickname"]')
                ?.innerText || "",
          })),
      };
    }
    function identityCheck(connection, proof) {
      if (
        !proof?.identity_verified ||
        !proof.authenticated_menu_verified ||
        proof.identity !== connection.identity ||
        proof.browser !== connection.browser ||
        proof.instance_id !== (connection.instance_id || "") ||
        !Number.isFinite(proof.observed_at) ||
        Math.abs(Date.now() / 1000 - proof.observed_at) > 300
      )
        throw new Error("TikTokDMFreshIdentityRequired");
    }
    async function read(page, connection, identityProof) {
      identityCheck(connection, identityProof);
      const snapshot = await frameFor(page).evaluate(
        readDOM,
        connection.identity,
      );
      return {
        ...snapshot,
        identity: connection.identity,
        browser: connection.browser,
        instance_id: connection.instance_id || "",
        identity_verified: true,
        authenticated_menu_verified: true,
        evidence:
          "Verified current account navigation in the same registered browser; read native inbox conversation and message IDs from DOM.",
      };
    }
    function collection(snapshot) {
      const keys = [
        "identity",
        "browser",
        "instance_id",
        "identity_verified",
        "authenticated_menu_verified",
        "observed_at",
        "evidence",
        "source_url",
        "inbox_verified",
        "coverage",
        "conversations",
      ];
      return Object.fromEntries(keys.map((k) => [k, snapshot[k]]));
    }
    async function collectVisible(page, connection, identityProof, limit = 10) {
      if (!Number.isInteger(limit) || limit < 1 || limit > 20)
        throw new Error("TikTokDMCollectionLimitInvalid");
      const first = await read(page, connection, identityProof),
        conversations = [];
      let skipped = 0;
      for (const card of first.available_conversations.slice(0, limit)) {
        if (!/^0:1:[0-9]{1,30}:[0-9]{1,30}$/.test(card.conversation_id || "")) {
          skipped++;
          continue;
        }
        const frame = frameFor(page),
          before = await read(page, connection, identityProof);
        if (before.conversation_id !== card.conversation_id) {
          const oldPeer = before.recipient_id,
            oldIds = before.conversations.flatMap((c) =>
              c.messages.map((m) => m.message_id).filter(Boolean),
            );
          await frame
            .locator(
              '[data-e2e="dm-new-conversation-item"][data-conv-id="' +
                card.conversation_id +
                '"]',
            )
            .click();
          try {
            await frame.waitForFunction(
              ({ id, oldPeer, oldIds }) => {
                const selected = document.querySelector(
                  '[data-e2e="dm-new-conversation-item"][aria-selected="true"]',
                );
                const peer = (
                  document.querySelector('[data-e2e="chat-uniqueid"]')
                    ?.textContent || ""
                )
                  .trim()
                  .replace(/^@/, "")
                  .toLowerCase();
                const ids = [
                  ...document.querySelectorAll(
                    '[data-e2e="dm-new-chat-item"] [data-e2e="dm-new-more-btn"]',
                  ),
                ].map((e) => e.id.replace(/^more-action-icon-msg-/, ""));
                return (
                  selected?.getAttribute("data-conv-id") === id &&
                  peer &&
                  peer !== oldPeer &&
                  ids.length > 0 &&
                  ids.every((i) => !oldIds.includes(i))
                );
              },
              { id: card.conversation_id, oldPeer, oldIds },
              { timeout: 12000 },
            );
          } catch (_) {
            skipped++;
            continue;
          }
        }
        const current = await read(page, connection, identityProof);
        if (
          current.conversation_id === card.conversation_id &&
          current.conversations.length === 1
        )
          conversations.push(current.conversations[0]);
        else skipped++;
      }
      return {
        snapshot: {
          ...collection(await read(page, connection, identityProof)),
          conversations,
          coverage: "partial",
        },
        skipped_conversations: skipped,
        visible_conversations: first.available_conversations.length,
      };
    }
    async function openRecipient(page, target) {
      const u = new URL(target);
      if (
        !trusted(target) ||
        u.search ||
        u.hash ||
        !/^\/@[A-Za-z0-9_.]+\/?$/.test(u.pathname)
      )
        throw new Error("TikTokDMRecipientProfileRequired");
      await page.goto(target, {
        waitUntil: "domcontentloaded",
        timeout: 45000,
      });
      const expected = u.pathname.replace(/^\/@/, "").replace(/\/$/, "");
      await page
        .getByRole("heading", { name: expected, exact: true })
        .waitFor({ state: "visible", timeout: 30000 });
      const message = page.getByRole("button", {
        name: "Message",
        exact: true,
      });
      if ((await message.count()) !== 1 || !(await message.isVisible()))
        throw new Error("TikTokDMInitiationNotAllowedByPage");
      await message.click(); // Opens only; no typing, request acceptance or sending.
      return { opened: true, recipient: expected, submission_started: false };
    }
    function contextCheck(s, item, connection, requireEditor = true) {
      const p = item.payload;
      if (
        s.identity !== connection.identity ||
        !s.inbox_verified ||
        !s.recipient_verified ||
        !s.conversation_verified ||
        s.recipient_id !== p.recipient_id ||
        (p.tiktok_conversation_id &&
          s.conversation_id !== p.tiktok_conversation_id)
      )
        throw new Error("TikTokDMRecipientOrConversationMismatch");
      if (
        p.action === "reply_direct_message" &&
        !(s.conversations[0]?.messages || []).some(
          (m) =>
            m.message_id === p.tiktok_message_id &&
            m.id_verified &&
            m.direction === "inbound",
        )
      )
        throw new Error("TikTokDMParentMessageUnverified");
      if (
        requireEditor &&
        (!s.editor_available || s.editor_text !== p.text || !s.can_message)
      )
        throw new Error("TikTokDMEditorOrSendControlUnavailable");
    }
    async function prepare(page, item, connection, identityProof) {
      const p = item.payload;
      if (
        !["send_direct_message", "reply_direct_message"].includes(p.action) ||
        !p.text?.trim() ||
        p.text.length > 1000 ||
        p.media?.length ||
        p.title
      )
        throw new Error("TikTokDMUnsupportedDraft");
      const before = await read(page, connection, identityProof);
      contextCheck(before, item, connection, false);
      if (before.editor_text && before.editor_text !== p.text)
        throw new Error("TikTokDMExistingDraft");
      const editor = frameFor(page).locator(
        '[data-e2e="dm-new-input-editor"] [role="textbox"][contenteditable="true"]',
      );
      if ((await editor.count()) !== 1)
        throw new Error("TikTokDMEditorAmbiguous");
      await editor.fill(p.text);
      const ready = await read(page, connection, identityProof);
      contextCheck(ready, item, connection);
      prepared.set(item.item_id, {
        conversation_id: ready.conversation_id,
        before_message_ids: before.conversations[0].messages
          .filter((m) => m.id_verified)
          .map((m) => m.message_id),
      });
      return ready;
    }
    function proof(snapshot, item) {
      const before = prepared.get(item.item_id);
      if (!before || before.conversation_id !== snapshot.conversation_id)
        throw new Error("TikTokDMPreparedContextRequired");
      const current =
        snapshot.conversations[0]?.messages
          .filter((m) => m.id_verified)
          .map((m) => m.message_id) || [];
      if (
        current.length !== before.before_message_ids.length ||
        current.some((id) => !before.before_message_ids.includes(id))
      )
        throw new Error("TikTokDMConversationChangedBeforeSubmit");
      return {
        ...snapshot,
        target: item.payload.target,
        payload_hash: item.payload_hash,
        before_message_ids: before.before_message_ids,
        parent_message_id: item.payload.tiktok_message_id || "",
        context_verified: true,
        can_reply: snapshot.can_message,
        no_duplicate: true,
        source_quote: "Private conversation with @" + snapshot.recipient_id,
      };
    }
    async function submit(page, item, connection, identityProof, permit) {
      if (
        !permit?.allowed ||
        !permit.submit_once ||
        permit.platform !== "tiktok" ||
        permit.item_id !== item.item_id ||
        permit.payload_hash !== item.payload_hash ||
        permit.target !== item.payload.target ||
        permit.identity !== connection.identity ||
        permit.browser !== connection.browser ||
        permit.instance_id !== (connection.instance_id || "") ||
        !Number.isFinite(permit.expires_at) ||
        permit.expires_at <= Date.now() / 1000 ||
        consumed.has(item.item_id)
      )
        throw new Error("TikTokDMInvalidOrUsedPermit");
      const snapshot = await read(page, connection, identityProof);
      contextCheck(snapshot, item, connection);
      const original = prepared.get(item.item_id);
      if (!original || original.conversation_id !== snapshot.conversation_id)
        throw new Error("TikTokDMPreparedContextRequired");
      const current =
        snapshot.conversations[0]?.messages
          .filter((m) => m.id_verified)
          .map((m) => m.message_id) || [];
      if (
        current.length !== original.before_message_ids.length ||
        current.some((id) => !original.before_message_ids.includes(id))
      )
        throw new Error("TikTokDMConversationChangedBeforeSubmit");
      const f = frameFor(page),
        control = snapshot.send_control;
      const button = control?.e2e
        ? f.locator('[data-e2e="' + control.e2e + '"]')
        : control?.label
          ? f.getByRole("button", { name: control.label, exact: true })
          : null;
      if (!button || (await button.count()) !== 1)
        throw new Error("TikTokDMSubmitAmbiguous");
      if (consumed.has(item.item_id) || permit.expires_at <= Date.now() / 1000)
        throw new Error("TikTokDMInvalidOrUsedPermit");
      consumed.add(item.item_id);
      await button.click({ timeout: 15000 });
      return {
        status: "unknown",
        verified: false,
        submission_started: true,
        reason: "TikTokDMIndependentReceiptRequired",
      };
    }
    async function verify(page, item, connection, identityProof, attempt) {
      const snapshot = await read(page, connection, identityProof);
      contextCheck(snapshot, item, connection, false);
      const original = attempt || prepared.get(item.item_id);
      if (!original || original.conversation_id !== snapshot.conversation_id)
        return { status: "unknown", verified: false };
      const matches = snapshot.conversations[0].messages.filter(
        (m) =>
          m.id_verified &&
          m.direction === "outbound" &&
          m.text === item.payload.text &&
          !original.before_message_ids.includes(m.message_id),
      );
      if (matches.length !== 1) return { status: "unknown", verified: false };
      const m = matches[0];
      return {
        status: "confirmed",
        verified: true,
        identity: connection.identity,
        recipient_id: snapshot.recipient_id,
        recipient_verified: true,
        conversation_id: snapshot.conversation_id,
        message_id: m.message_id,
        message_id_verified: true,
        observed_at: snapshot.observed_at,
        published_at: m.occurred_at,
        direction: "outbound",
        observed_text: m.text,
        external_reference: snapshot.source_url,
        parent_target: item.payload.target,
        parent_message_id: item.payload.tiktok_message_id || "",
        author_verified: true,
        parent_verified: true,
        independent_read: true,
        evidence:
          "Independently reread the bound conversation: native new outgoing message ID, exact text, sender and recipient matched.",
      };
    }
    return {
      readDOM,
      frameFor,
      read,
      collection,
      collectVisible,
      openRecipient,
      prepare,
      proof,
      submit,
      verify,
    };
  })());
if (typeof module !== "undefined") module.exports = tiktokMessages;

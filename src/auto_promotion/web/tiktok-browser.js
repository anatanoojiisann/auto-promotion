"use strict";

async function tiktokBrowserPanel() {
  const [status, work] = await Promise.all([
    api("/tiktok-browser/status"),
    api("/tiktok-browser/jobs"),
  ]);
  openPanel(
    "TikTok 网页账号",
    `
    <p class="help">沿用已登录的 Tabbit、Chrome 或 Edge。登记后由桌面执行端现场核对身份；评论与回复需另行创建并启动本机任务。</p>
    <form id="tiktok-browser-connect"><div class="field-grid">
      <label>账号名称<input name="label" required maxlength="80" placeholder="TikTok · 品牌账号"></label>
      <label>本机标签<input name="backend_label" required pattern="[A-Za-z0-9_-]{1,60}" placeholder="tiktok-web-brand"></label>
      <label>TikTok 用户名<input name="username" required placeholder="不含 @" maxlength="24"></label>
      <label>现有浏览器<select name="browser"><option value="tabbit">Tabbit</option><option value="chrome">Chrome</option><option value="edge">Edge</option></select></label>
      <label class="full">浏览器实例标识<input name="instance_id" maxlength="100" placeholder="由桌面执行端读取；Tabbit 填实例 ID"></label>
    </div><div class="form-actions"><button type="submit">登记网页账号</button></div></form>
    <h3>已登记连接</h3>
    ${status.accounts.map((a) => `<article class="item-preview"><strong>${esc(a.label)}</strong><p>@${esc(a.identity)} · ${esc(a.browser)} · ${a.identity_observation_fresh ? "身份已核验" : "等待桌面身份核验"}</p><p class="help">${esc(a.blocked_reason || a.profile_url)}</p><button class="secondary" data-private-inbox="${a.account_id}">私信收件箱</button><p class="help">${a.direct_messages_enabled ? "已接入私信任务，发送仍需明确启动" : "等待桌面采集私信并启用任务能力"}</p></article>`).join("") || '<p class="muted">尚未登记 TikTok 网页账号。</p>'}
    <h3>桌面执行状态</h3><p>待执行 ${work.jobs.length} · 待核验 ${work.unresolved.length} · 待观察回复 ${work.feedback_jobs.length}</p>
    ${work.jobs.map((j) => `<article class="item-preview"><strong>${esc(j.label)}</strong><p>${esc(j.payload.text)}</p><p class="help">${esc(j.payload.target)} · ${j.blocker ? esc(j.blocker) : j.due ? "等待桌面执行端" : "尚未到计划时间"}</p></article>`).join("")}
    <p class="help">首版支持指定视频的文字评论／回复。遇到登录、验证码、页面限制或提交结果不明确时，任务保留等待或待核验状态。</p>`,
  );
  $("#tiktok-browser-connect").onsubmit = (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    for (const k of Object.keys(data)) data[k] = data[k].trim();
    busy(event.target.querySelector("button"), async () => {
      await api("/tiktok-browser/connect", "POST", data);
      await refresh();
      renderAccounts();
      await tiktokBrowserPanel();
      toast("已登记；等待桌面执行端核实当前登录身份");
    });
  };
  document
    .querySelectorAll("[data-private-inbox]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          tiktokPrivateInbox(b.dataset.privateInbox).catch((e) =>
            toast(e.message, true),
          )),
    );
}

function tiktokBrowserEditor(row, item) {
  const account = state.accounts.find((a) => a.id === item.account_id);
  if (account?.platform !== "tiktok" || account.execution_backend !== "desktop")
    return;
  row.querySelector('[name="title"]').closest("div").hidden = true;
  row.querySelector('[name="target_owned"]').closest("div").hidden = true;
  row.querySelector('input[type="file"]').closest("div").hidden = true;
  row.querySelector('[name="execution_mode"]').closest("div").hidden = true;
  const dm = ["send_direct_message", "reply_direct_message"].includes(
    item.action,
  );
  row.querySelector('[name="text"]').maxLength = dm ? 1000 : 150;
  const target = row.querySelector('[name="target"]');
  if (dm) {
    target.placeholder = "https://www.tiktok.com/@收件人用户名";
    const update = () => {
      try {
        const u = new URL(target.value);
        const m = /^\/@([A-Za-z0-9_.]+)\/?$/.exec(u.pathname);
        if (m)
          row.querySelector('[name="dm_recipient"]').value = m[1].toLowerCase();
      } catch (_) {}
    };
    target.addEventListener("input", update);
    update();
    return;
  }
  target.placeholder =
    "https://www.tiktok.com/@作者/video/视频ID（回复需带 ?comment_id=评论ID）";
  const sync = () => {
    try {
      const url = new URL(target.value);
      const m = /^\/@[A-Za-z0-9_.]+\/video\/([0-9]{1,30})\/?$/.exec(
        url.pathname,
      );
      if (
        !m ||
        !["www.tiktok.com", "tiktok.com"].includes(url.hostname) ||
        url.protocol !== "https:"
      )
        return;
      row.querySelector('[name="tiktok_video_id"]').value = m[1];
      row.querySelector('[name="tiktok_comment_id"]').value =
        url.searchParams.get("comment_id") || "";
    } catch (_) {
      /* Server validates the complete target when saving. */
    }
  };
  target.addEventListener("input", sync);
  sync();
}

async function tiktokPrivateInbox(accountId) {
  const data = await api(`/tiktok-browser/accounts/${accountId}/dm/inbox`);
  openPanel(
    "TikTok 私信收件箱",
    `<p class="help">本机已采集记录，覆盖范围：${data.collection ? (data.collection.coverage === "complete" ? "已核验完整范围" : "部分记录") : "尚未采集"}。刷新列表不自动读取平台，也不发送回复。</p><button id="new-private-draft">准备主动私信</button><div>${data.conversations.map((c, i) => `<article class="item-preview"><strong>@${esc(c.peer_username)}</strong><p>${c.message_count} 条已采集记录</p><button class="secondary" data-open-private="${i}">查看消息</button></article>`).join("") || '<p class="muted">等待桌面执行端采集并核实会话。</p>'}</div><div id="private-message-list"></div>`,
  );
  $("#new-private-draft").onclick = () => tiktokPrivateDraft(accountId);
  document.querySelectorAll("[data-open-private]").forEach(
    (button) =>
      (button.onclick = () =>
        busy(button, async () => {
          const conversation =
            data.conversations[Number(button.dataset.openPrivate)];
          const result = await api(
            `/tiktok-browser/accounts/${accountId}/dm/messages?conversation_id=${encodeURIComponent(conversation.conversation_id)}`,
          );
          $("#private-message-list").innerHTML =
            `<h3>@${esc(conversation.peer_username)}</h3>` +
            result.messages
              .map(
                (m, i) =>
                  `<article class="item-preview"><p class="help">${m.direction === "outbound" ? "我方发送" : m.direction === "inbound" ? "对方发送" : "方向待核验"} · ${esc(m.timestamp_label || "平台时间未返回")}</p><div class="item-text">${esc(m.text)}</div>${m.direction === "inbound" && m.id_verified ? `<button class="secondary" data-prepare-private="${i}">准备回复</button>` : ""}</article>`,
              )
              .join("");
          document
            .querySelectorAll("[data-prepare-private]")
            .forEach(
              (b) =>
                (b.onclick = () =>
                  tiktokPrivateDraft(
                    accountId,
                    conversation,
                    result.messages[Number(b.dataset.preparePrivate)],
                  )),
            );
        })),
  );
}

function tiktokPrivateDraft(accountId, conversation = null, parent = null) {
  openPanel(
    parent ? "准备私信回复" : "准备主动私信",
    `<form id="private-draft"><label>收件人 TikTok 用户名<input name="recipient" required pattern="[A-Za-z0-9_.]{1,24}" value="${esc(conversation?.peer_username || "")}" ${parent ? "readonly" : ""}></label><label>完整私信文案<textarea name="text" maxlength="1000" required></textarea></label><p class="help">保存为草稿，需在任务中心明确启动后，桌面执行端再核对收件人和平台权限。</p><button type="submit">保存私信草稿</button></form>`,
  );
  $("#private-draft").onsubmit = (event) => {
    event.preventDefault();
    busy(event.target.querySelector("button"), async () => {
      const f = new FormData(event.target),
        peer = String(f.get("recipient")).trim().toLowerCase(),
        target = "https://www.tiktok.com/@" + peer;
      const now = Date.now(),
        scheduled = new Date(now + 120000).toISOString();
      const task = await api("/tasks", "POST", {
        name: "TikTok 私信 · @" + peer,
        account_ids: [accountId],
        policy: {
          starts_at: new Date(now - 60000).toISOString(),
          ends_at: new Date(now + 86400000).toISOString(),
          execution_channel: "local",
          target_allowlist: [target],
        },
      });
      await api(`/tasks/${task.id}/items`, "PUT", [
        {
          account_id: accountId,
          action: parent ? "reply_direct_message" : "send_direct_message",
          target,
          text: String(f.get("text")),
          recipient_id: peer,
          tiktok_conversation_id: conversation?.conversation_id || "",
          tiktok_message_id: parent?.message_id || "",
          scheduled_at: scheduled,
        },
      ]);
      await refresh();
      $("#panel").close();
      location.hash = "#tasks/" + task.id;
      toast("已保存草稿，尚未启动发送");
    });
  };
}

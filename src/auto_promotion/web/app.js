"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const state = {
  accounts: [],
  connections: [],
  overview: {},
  tasks: [],
  current: null,
  filter: "all",
};
const names = {
  telegram: "Telegram",
  reddit: "Reddit",
  instagram: "Instagram",
  x: "X",
  discord: "Discord",
  tiktok: "TikTok",
};
const actions = {
  discover: "搜索发现",
  join: "入群",
  send_message: "发送文字",
  reply: "回复",
  send_file: "发送文件",
  schedule_message: "原生定时文字",
  schedule_file: "原生定时文件",
  post: "发布原创帖子",
  publish: "发布媒体",
  manual_comment: "人工评论",
  like: "点赞",
  repost: "转发",
  quote: "引用",
  comment: "评论自有内容",
  unlike: "取消评论点赞",
  hide_comment: "隐藏评论",
  unhide_comment: "恢复评论显示",
  delete_comment: "删除自己的评论",
};
const statuses = {
  draft: "草稿",
  active: "进行中",
  paused: "已暂停",
  cancelled: "已取消",
  completed: "已完成",
  healthy: "已核验",
  unchecked: "未检查",
  unavailable: "需检查",
  temporary_unavailable: "暂时探测失败",
  navigation_blocked: "浏览器导航受阻",
  authorization_required: "身份或授权需核验",
  unverified: "尚未核验",
  cooldown: "冷却中",
  disabled: "已停用",
  busy: "账号正在使用中",
  not_attempted: "待执行",
  executing: "执行中",
  confirmed: "已发布 · 已核验",
  scheduled: "平台排期已核验",
  unknown: "结果未知",
  failed_known: "执行失败",
  manual_attention: "待人工处理",
  missed: "已错过时间",
  qualified: "合格",
  rejected: "已拒绝",
};
const phases = {
  discovery: "准备任务",
  discovered: "准备外发清单",
  review: "等待开始发送",
  starting: "正在准备发送",
  ready: "执行与核验",
};
const connectionStages = {
  connecting: "正在连接",
  awaiting_login: "等待浏览器登录",
  checking: "正在核验连接",
  connected: "已连接 · 身份已核验",
  needs_check: "登录已保存 · 需检查连接",
  cancelled: "已取消连接",
  interrupted: "连接被工作台重启中断",
  error: "连接未完成",
};
const connectionErrors = {
  InstagramIdentityMismatch: "令牌所属账号与填写的用户名不一致。",
  InstagramRequestRejected:
    "Instagram 拒绝了验证请求，请核对令牌、API 版本及应用权限。",
  InstagramRateLimited: "Instagram 请求受到限流，请稍后再试。",
  BrowserIdentityMismatch: "浏览器登录账号与填写的用户名不一致。",
  ChangedIdentityRequiresNewAccountLabel:
    "此本机标签已绑定另一身份，请使用新的账号标签。",
  LoginWaitingTimedOut: "等待登录超时，请重新连接。",
  LoginBrowserClosed: "登录浏览器已关闭，请重新连接。",
  AccountBusy: "账号正被其他操作使用，请稍后再连接。",
  DiscordInvalidToken:
    "Bot Token 无效或已过期，请从 Discord 开发者后台重新获取。",
  DiscordBotAccountRequired: "请使用官方 Bot Token。",
  DiscordIdentityMismatch: "此标签绑定的 Bot 与当前 Token 不一致。",
  DiscordRateLimited: "Discord 请求受到限流，请在冷却结束后重试。",
  DiscordMissingPermissions: "Bot 缺少频道访问、发送或读取历史权限。",
  DiscordTransportUnavailable: "无法连接 Discord，请检查本机网络。",
};
const fmt = (v) =>
  v
    ? new Date(typeof v === "number" ? v * 1000 : v).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "—";
const isoLocal = (v) =>
  new Date(v - new Date().getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
const api = async (path, method = "GET", body, extraHeaders = {}) => {
  const response = await fetch("/api" + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-AP-Client": "local-ui",
      ...extraHeaders,
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const result = await response.json();
  if (!response.ok)
    throw Error(result.error || JSON.stringify(result.detail) || "请求失败");
  return result;
};
function toast(text, error = false) {
  if (typeof tiktokErrorMessage === "function") text = tiktokErrorMessage(text);
  if (error && $("#panel").open) {
    let alert = $("#panel-content .form-error");
    if (!alert) {
      alert = document.createElement("div");
      alert.className = "form-error";
      alert.setAttribute("role", "alert");
      $("#panel-content").prepend(alert);
    }
    alert.textContent = text;
  }
  const el = $("#notice");
  el.textContent = text;
  el.className = "visible" + (error ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.className = ""), 6500);
}
function badge(value) {
  const color = ["healthy", "confirmed", "completed", "qualified"].includes(
    value,
  )
    ? "green"
    : [
          "unknown",
          "manual_attention",
          "missed",
          "paused",
          "unchecked",
          "temporary_unavailable",
          "navigation_blocked",
          "unverified",
          "cooldown",
          "disabled",
        ].includes(value)
      ? "amber"
      : [
            "failed_known",
            "unavailable",
            "rejected",
            "authorization_required",
          ].includes(value)
        ? "red"
        : ["scheduled", "active", "executing"].includes(value)
          ? "blue"
          : "";
  return `<span class="badge ${color}">${esc(statuses[value] || value)}</span>`;
}
function platform(p) {
  return `<span class="platform"><span class="platform-mark ${esc(p)}">${esc({ telegram: "TG", reddit: "R", instagram: "IG", x: "X", discord: "DC", tiktok: "TT" }[p])}</span>${esc(names[p])}</span>`;
}
function heading(title, desc, button = "") {
  return `<div class="page-heading"><div><h1>${title}</h1><p>${desc}</p></div>${button}</div>`;
}
function empty(title, body, button = "") {
  return `<div class="empty"><h2>${title}</h2><p>${body}</p>${button}</div>`;
}
let panelGeneration = 0;
function openPanel(title, html) {
  const generation = ++panelGeneration;
  $("#panel-title").textContent = title;
  $("#panel-content").innerHTML = html;
  $("#panel").showModal();
  $("#panel-content input")?.focus();
  return generation;
}
function closePanel() {
  ++panelGeneration;
  $("#panel").close();
}
$("#close-panel").onclick = closePanel;
$("#panel").addEventListener("cancel", () => ++panelGeneration);
const busyOperations = new WeakMap();
function busy(button, fn, isCurrent) {
  if (busyOperations.has(button)) return busyOperations.get(button);
  const generation = panelGeneration;
  const route = location.hash;
  isCurrent ||= () => generation === panelGeneration && route === location.hash;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  const operation = Promise.resolve()
    .then(fn)
    .catch((e) => {
      if (isCurrent()) toast(e.message, true);
    })
    .finally(() => {
      busyOperations.delete(button);
      button.disabled = false;
      if (button.textContent === "处理中…") button.textContent = label;
    });
  busyOperations.set(button, operation);
  return operation;
}
async function refresh() {
  [state.accounts, state.overview, state.tasks, state.connections] =
    await Promise.all([
      api("/accounts"),
      api("/overview"),
      api("/tasks"),
      api("/connections"),
    ]);
  $("#write-mode").textContent = state.overview.live_writes
    ? "真实连接已启用"
    : "真实发布关闭";
  $("#global-stop").textContent = state.overview.global_stop
    ? "任务执行已暂停 · 恢复"
    : "暂停全部任务";
  $("#global-stop").className =
    "secondary compact" + (state.overview.global_stop ? "" : " danger");
  $("#worker-state").textContent =
    (state.overview.worker
      ? state.overview.sender?.status === "running"
        ? "发送进程运行中"
        : "发送进程未确认在线"
      : state.overview.report_worker
        ? "报告调度运行中 · 外发手动运行"
        : "手动运行模式") + " · Asia/Shanghai";
}
$("#global-stop").onclick = (e) =>
  busy(e.target, async () => {
    await api("/control/stop", "POST", {
      enabled: !state.overview.global_stop,
    });
    await refresh();
    await render();
    toast(
      state.overview.global_stop
        ? "已暂停未提交任务；平台原生排期需单独核对"
        : "已恢复已启动任务的执行",
    );
  });
async function render() {
  const hash = location.hash.slice(1) || "tasks";
  const section = hash.split("/")[0];
  document.querySelectorAll("[data-nav]").forEach((el) => {
    const on = el.dataset.nav === section;
    el.classList.toggle("active", on);
    el.setAttribute("aria-current", on ? "page" : "false");
  });
  $("#breadcrumb").textContent =
    "工作区 / " +
    ({
      accounts: "账号中心",
      tasks: "任务中心",
      api: "API 中心",
      statistics: "互动统计",
      pools: "号池与人设",
      content: "共享内容池",
      campaigns: "推广活动",
      growth: "业务增长",
      inbox: "回复待办",
      feeds: "内容采集来源",
      templates: "任务模板",
      interactions: "高频互动",
    }[section] || "任务中心");
  if (hash.startsWith("tasks/")) return renderTask(hash.slice(6));
  if (hash.startsWith("campaigns/")) return renderCampaign(hash.slice(10));
  if (hash.startsWith("growth/")) return renderGrowthGoal(hash.slice(7));
  if (section === "interactions") return renderInteractions(hash.split("/")[1]);
  if (section === "growth") return renderGrowth();
  if (section === "inbox") return renderInbox();
  if (section === "feeds") return renderFeeds();
  if (section === "pools") return renderPools();
  if (section === "content") return renderContent();
  if (section === "campaigns") return renderCampaigns();
  if (section === "templates") return renderTaskTemplates();
  if (section === "accounts") return renderAccounts();
  if (section === "api") return renderAPI();
  if (section === "statistics") return renderStatistics();
  return renderTasks();
}

async function renderStatistics(day = "") {
  const [snapshot, history] = await Promise.all([
    api("/statistics" + (day ? "?day=" + encodeURIComponent(day) : "")),
    api("/statistics/reports"),
  ]);
  const metric = (v) => (v === null ? "不可统计" : esc(v));
  const labels = {
    interval: "每三小时",
    daily_20: "20:00 当日累计",
    daily_final: "完整自然日",
  };
  const feedback = {
    unavailable: "尚未采集",
    partial: "采集不完整",
    failed: "采集失败",
    complete: "已完整采集",
  };
  const total = snapshot.totals;
  const selectedDay =
    day ||
    new Date(snapshot.since * 1000).toLocaleDateString("sv-SE", {
      timeZone: "Asia/Shanghai",
    });
  $("#main").innerHTML =
    heading(
      "互动统计",
      "汇总全部真实任务，按北京时间统计已核验的实际发布。",
      '<button id="refresh-statistics" class="secondary">刷新统计</button>',
    ) +
    `<div class="toolbar"><label for="statistics-day">统计日期</label><input id="statistics-day" type="date" value="${esc(selectedDay)}" max="${esc(new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" }))}"><span class="muted">截至 ${esc(fmt(snapshot.until))}</span></div>
    <div class="stats"><span class="stat"><strong>${total.published_success}</strong>已核验发布</span><span class="stat"><strong>${metric(total.responded_interactions)}</strong>有他人回复的互动</span><span class="stat"><strong>${metric(total.received_replies)}</strong>收到回复总条数</span><span class="stat"><strong>${total.pending_verification}</strong>所有日期待核验</span></div>
    <div class="banner warning">已排期与结果未知不计入发布成功。回复尚未采集时显示“不可统计”；已核验但缺少实际发布时间的 ${total.confirmed_without_publication_time} 条另列待补时间。</div>
    <div class="surface"><div class="table-wrap" role="region" aria-label="各平台互动统计，可横向滚动" tabindex="0"><table><thead><tr><th>平台</th><th>发布成功</th><th>当日已排期</th><th>当日未知</th><th>有回复的互动</th><th>收到回复</th><th>回复率</th><th>数据覆盖</th></tr></thead><tbody>${snapshot.platforms.map((p) => `<tr><td>${platform(p.platform)}</td><td>${p.published_success}<span class="sub">待补发布时间 ${p.confirmed_without_publication_time}</span></td><td>${p.planned_statuses.scheduled}</td><td>${p.planned_statuses.unknown}</td><td>${metric(p.responded_interactions)}</td><td>${metric(p.received_replies)}</td><td>${p.reply_rate === null ? "—" : (p.reply_rate * 100).toFixed(1) + "%"}</td><td>${p.live_accounts ? esc(feedback[p.feedback_status]) : "未接入真实账号"}${p.observed_replies ? `<span class="sub">已观察到 ${p.observed_replies} 条回复</span>` : ""}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="surface padded mt-24"><h2>定时报告</h2><p class="help">每三小时保存一次；20:00 保存截至当时的累计，次日 00:00 补齐完整自然日。恢复运行后补跑遗漏报告，迟到核验会更新原报告版本。</p><p class="muted">${history.scheduler_running ? "报告调度运行中" : "报告调度未运行，启动工作台调度器后自动恢复"}</p>${history.reports.length ? `<div class="table-wrap" role="region" aria-label="历史统计报告，可横向滚动" tabindex="0"><table><thead><tr><th>截止时间</th><th>报告</th><th>期间发布</th><th>当日累计发布</th><th>期间收到回复</th><th>版本</th></tr></thead><tbody>${history.reports.map((r) => `<tr><td>${esc(fmt(r.cutoff))}</td><td>${esc(labels[r.kind])}</td><td>${r.content.period.totals.published_success}</td><td>${r.content.daily.totals.published_success}</td><td>${metric(r.content.period.totals.received_replies)}</td><td>${r.revision}</td></tr>`).join("")}</tbody></table></div>` : '<p class="muted">尚无已保存的报告。</p>'}</div>`;
  $("#refresh-statistics").onclick = (e) =>
    busy(e.target, () => renderStatistics($("#statistics-day").value));
  $("#statistics-day").onchange = (e) =>
    renderStatistics(e.target.value).catch((error) =>
      toast(error.message, true),
    );
}
function renderAccounts() {
  const checked = state.accounts.filter((a) => a.status === "healthy").length;
  $("#main").innerHTML =
    heading(
      "账号中心",
      "连接账号，确认能力，让每一次执行都有明确的身份。",
      '<div class="actions"><button class="secondary" id="connect-tiktok">连接 TikTok</button><button class="secondary" id="connect-discord">连接 Discord</button><button class="secondary" id="connect-instagram">连接 IG</button><button class="secondary" id="connect-x">连接 X</button><button id="new-account">添加账号</button></div>',
    ) +
    `<div class="stats"><span class="stat"><strong>${state.accounts.length}</strong>已登记账号</span><span class="stat"><strong>${checked}</strong>通过身份核验</span><span class="stat"><strong>${state.accounts.filter((a) => a.mode === "live").length}</strong>真实账号</span></div><div id="connection-list"></div><div class="toolbar"><div class="filter-tabs"><button class="selected" data-mode="all">全部账号</button><button data-mode="live">真实账号</button><button data-mode="simulation">模拟账号</button></div><span class="spacer"></span><button class="secondary compact" id="check-all">检查全部连接</button></div><div class="surface" id="account-list"></div><p class="connect-note">TikTok、Discord、IG 和 X 可在这里完成连接，身份核验后自动登记。登录不启用推广发布；外发在明确启动任务后执行。</p>`;
  $("#connect-tiktok").onclick = () =>
    tiktokSetup().catch((e) => toast(e.message, true));
  $("#connect-instagram").onclick = () => connectionForm("instagram");
  $("#connect-x").onclick = () => connectionForm("x");
  $("#connect-discord").onclick = discordConnectionForm;
  $("#new-account").onclick = accountForm;
  $("#check-all").onclick = (e) =>
    busy(e.target, async () => {
      for (const a of state.accounts)
        await api(`/accounts/${a.id}/check`, "POST");
      await refresh();
      renderAccounts();
      toast("连接检查完成");
    });
  document.querySelectorAll("[data-mode]").forEach(
    (b) =>
      (b.onclick = () => {
        document
          .querySelectorAll("[data-mode]")
          .forEach((x) => x.classList.toggle("selected", x === b));
        accountRows(b.dataset.mode);
      }),
  );
  accountRows("all");
  connectionRows();
}
function connectionRows() {
  const list = $("#connection-list");
  if (!list) return;
  list.innerHTML = state.connections
    .slice(-4)
    .reverse()
    .map((job) => {
      const account = state.accounts.find(
        (a) =>
          a.mode === "live" &&
          a.platform === job.platform &&
          a.backend_label === job.backend_label,
      );
      const recovered =
        ["error", "cancelled", "needs_check", "interrupted"].includes(
          job.stage,
        ) &&
        account?.status === "healthy" &&
        account.checked_at >= job.created_at;
      const stage = recovered ? "connected" : job.stage;
      const active = ["connecting", "awaiting_login", "checking"].includes(
        job.stage,
      );
      const help = recovered
        ? "此前的登录流程已结束；该账号现已通过身份与连接核验。"
        : job.stage === "awaiting_login"
          ? "请在打开的独立浏览器中完成登录和验证，工作台会自动核验身份。"
          : job.stage === "connected"
            ? "账号已加入账号中心，可以创建任务。"
            : job.stage === "needs_check"
              ? "本机登录已保存，请使用账号行中的“检查”再次核验。"
              : job.stage === "interrupted"
                ? "工作台重启中断了连接，请检查账号或重新连接。"
                : job.stage === "error"
                  ? connectionErrors[job.reason] ||
                    "连接未完成：" + (job.reason || "ConnectionFailed")
                  : "";
      return `<div class="banner ${stage === "error" || stage === "needs_check" ? "warning" : ""}"><div><strong>${esc(names[job.platform])} · @${esc(job.username)} — ${esc(connectionStages[stage] || stage)}</strong>${help ? `<p>${esc(help)}</p>` : ""}${job.instagram_user_id ? `<p>自有发布目标 ID：${esc(job.instagram_user_id)}</p>` : ""}</div>${active ? `<button class="secondary compact" data-cancel-connection="${esc(job.id)}">取消连接</button>` : ""}</div>`;
    })
    .join("");
  document.querySelectorAll("[data-cancel-connection]").forEach((button) => {
    button.onclick = () =>
      busy(button, async () => {
        await api(
          `/connections/${button.dataset.cancelConnection}/cancel`,
          "POST",
        );
        toast("正在取消连接");
      });
  });
}
function discordConnectionForm() {
  openPanel(
    "连接 Discord Bot",
    `<form id="discord-connection-form"><p class="help">在 <a href="https://discord.com/developers/applications" target="_blank" rel="noreferrer">Discord 开发者后台</a> 创建 Bot，并由服务器管理员安装。授予查看频道、发送消息和读取消息历史权限；上传媒体还需附件权限。</p><div class="field-grid"><div class="full"><label for="discord-token">Discord Bot Token</label><input id="discord-token" name="bot_token" type="password" required autocomplete="off"><span class="help">只在本机保存；身份会自动核验。</span></div><div class="full"><label for="discord-label">本机账号标签</label><input id="discord-label" name="backend_label" value="discord-bot" required autocomplete="off"><span class="help">多个 Bot 使用不同标签；更新 Token 时填写原标签。</span></div></div><p class="help">发布范围为 Bot 已获授权的服务器内普通文字和公告频道，频道需对服务器成员公开。定时发送由工作台执行，届时电脑和服务需要运行。</p><div class="form-actions"><button type="submit">验证并连接 Discord</button></div></form>`,
  );
  $("#discord-connection-form").onsubmit = (event) => {
    event.preventDefault();
    const form = event.target;
    const payload = {
      platform: "discord",
      bot_token: form.elements.bot_token.value,
      backend_label: form.elements.backend_label.value.trim(),
    };
    form.elements.bot_token.value = "";
    busy(event.submitter, async () => {
      try {
        await api("/connections", "POST", payload);
        $("#panel").close();
        await refresh();
        renderAccounts();
        toast("正在验证 Discord Bot 身份");
      } finally {
        delete payload.bot_token;
      }
    });
  };
}
function connectionForm(selectedPlatform) {
  const isInstagram = selectedPlatform === "instagram";
  openPanel(
    "连接 " + names[selectedPlatform],
    `<form id="connection-form"><p class="help">${isInstagram ? '使用商业或创作者账号的 Instagram 用户访问令牌。可在 <a href="https://developers.facebook.com/apps/" target="_blank" rel="noreferrer">Meta 应用控制台</a> 的 Instagram API 设置中获取。个人账号需先由你在 Instagram 中切换为专业账号。' : "会打开本机独立浏览器，请在该窗口完成 X 登录。工作台自动核验用户名并保存登录。"}</p><div class="field-grid"><div class="full"><label for="connection-username">平台用户名</label><input id="connection-username" name="username" required autocomplete="off" placeholder="用户名，不含 @"></div>${isInstagram ? '<div class="full"><label for="connection-token">Instagram 用户访问令牌</label><input type="password" id="connection-token" name="access_token" required autocomplete="off"><span class="help">只在本机输入；令牌不会显示在连接状态或操作日志中。</span></div><div class="full"><label for="connection-version">Meta 应用 API 版本</label><input id="connection-version" name="api_version" placeholder="例如 v25.0，以应用控制台为准" pattern="v[0-9]+\\.[0-9]+" required></div>' : '<div class="full"><label for="connection-browser">登录浏览器</label><select id="connection-browser" name="browser_channel"><option value="chromium">Chromium</option><option value="msedge">Microsoft Edge</option><option value="chrome">Google Chrome</option></select><span class="help">使用独立登录窗口；Microsoft Edge／Google Chrome 需已安装。</span></div>'}<div class="full"><label for="connection-label">本机账号标签（可选）</label><input id="connection-label" name="backend_label" autocomplete="off" placeholder="留空自动生成；已有账号填原标签"><span class="help">重新登录已有账号时填写账号原标签。</span></div></div><div class="form-actions"><button type="submit">${isInstagram ? "验证并连接 IG" : "打开 X 登录"}</button></div></form>`,
  );
  $("#connection-form").onsubmit = (event) => {
    event.preventDefault();
    const form = event.target;
    const payload = {
      platform: selectedPlatform,
      username: form.elements.username.value.trim(),
      ...(form.elements.backend_label.value.trim()
        ? { backend_label: form.elements.backend_label.value.trim() }
        : {}),
      ...(isInstagram
        ? {
            access_token: form.elements.access_token.value,
            api_version: form.elements.api_version.value.trim(),
          }
        : { browser_channel: form.elements.browser_channel.value }),
    };
    if (isInstagram) form.elements.access_token.value = "";
    busy(event.submitter, async () => {
      try {
        await api("/connections", "POST", payload);
        $("#panel").close();
        await refresh();
        renderAccounts();
        toast(
          isInstagram ? "正在验证 Instagram 身份" : "正在打开 X 登录浏览器",
        );
      } finally {
        delete payload.access_token;
      }
    });
  };
}
let connectionPolling = false;
setInterval(async () => {
  if (connectionPolling || !location.hash.startsWith("#accounts")) return;
  if (
    !state.connections.some((j) =>
      ["connecting", "awaiting_login", "checking"].includes(j.stage),
    )
  )
    return;
  connectionPolling = true;
  try {
    const previous = state.connections.map((j) => j.stage).join(",");
    state.connections = await api("/connections");
    connectionRows();
    if (state.connections.map((j) => j.stage).join(",") !== previous) {
      await refresh();
      if (location.hash.startsWith("#accounts") && $("#account-list")) {
        accountRows(
          document.querySelector("[data-mode].selected")?.dataset.mode || "all",
        );
      }
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    connectionPolling = false;
  }
}, 2000);
function accountRows(mode) {
  const rows = state.accounts.filter((a) => mode === "all" || a.mode === mode);
  $("#account-list").innerHTML = rows.length
    ? `<div class="table-wrap" role="region" aria-label="数据表格，可横向滚动" tabindex="0"><table><thead><tr><th>账号 / 平台</th><th>连接状态</th><th>可接入能力</th><th>执行边界</th><th>操作</th></tr></thead><tbody>${rows.map((a) => `<tr><td>${platform(a.platform)}<span class="sub">${esc(a.label)} · ${a.mode === "simulation" ? "模拟" : "真实"}</span></td><td>${badge(a.status)}${a.disabled ? ' <span class="badge amber">已停用</span>' : ""}<span class="sub">${a.checked_at ? fmt(a.checked_at) : "尚未检查"}${a.cooldown_until > Date.now() / 1000 ? "<br>冷却至 " + fmt(a.cooldown_until) : ""}</span></td><td><div class="chips">${a.capabilities.map((c) => `<span class="chip">${actions[c] || c}</span>`).join("")}</div></td><td>${a.daily_limit} 条 / 日<span class="sub">间隔 ${a.min_interval} 秒</span></td><td><div class="actions"><button class="secondary compact" data-check="${a.id}">检查</button><button class="text-button" data-disable="${a.id}" data-value="${!a.disabled}">${a.disabled ? "启用" : "停用"}</button></div></td></tr>`).join("")}</tbody></table></div>`
    : empty("暂无账号", "添加现有平台账号，或创建模拟账号测试完整流程。");
  for (const account of rows) {
    const control = document.querySelector(`[data-disable="${account.id}"]`);
    const hourly = document.createElement("span");
    hourly.className = "sub";
    hourly.textContent = `${account.hourly_limit} 条 / 小时`;
    control.closest("tr").cells[3].append(hourly);
    const statusBadge = control.closest("tr").cells[1].querySelector(".badge");
    if (statusBadge && account.connection_state) {
      statusBadge.outerHTML = badge(account.connection_state);
    }
    if (account.health?.last_verified_at) {
      const previous = document.createElement("span");
      previous.className = "sub";
      previous.textContent =
        "最近身份核验 " +
        fmt(account.health.last_verified_at) +
        "；发送前仍重新核验";
      control.closest("tr").cells[1].append(previous);
    }
    if (account.status === "unavailable" && account.health?.reason) {
      const reason = document.createElement("span");
      reason.className = "sub";
      reason.textContent =
        {
          DuplicatePlatformIdentity: "同一平台身份已由另一账号标签绑定",
          AccountIdentityChanged: "登录身份发生变化，请核对账号",
          AccountIdentityUnavailable: "无法核对原绑定身份",
          BrowserNavigationAccessDenied:
            "后台页面被拒绝（401/403），尚未到身份检查；不代表已退出登录",
          BrowserNavigationDownload: "后台导航触发下载，尚未到身份检查",
          BrowserNavigationTimeout: "后台页面导航超时，尚未到身份检查",
        }[account.health.reason] || account.health.reason;
      control.closest("tr").cells[1].append(reason);
    }
    if (account.health?.next_check_at > Date.now() / 1000) {
      const retry = document.createElement("span");
      retry.className = "sub";
      retry.textContent = "探测最早刷新 " + fmt(account.health.next_check_at);
      control.closest("tr").cells[1].append(retry);
    }
    if (account.outbound_block) {
      const blocked = document.createElement("div");
      blocked.className = "sub";
      blocked.textContent =
        "外发已暂停：" +
        outboundBlockReason(account.outbound_block.reason) +
        "；自 " +
        fmt(account.outbound_block.since);
      control.closest("tr").cells[1].append(blocked);
      const recover = document.createElement("button");
      recover.className = "secondary compact";
      recover.textContent = "处理外发暂停";
      recover.onclick = () => accountBlockForm(account, mode);
      control.insertAdjacentElement("beforebegin", recover);
    }
    const button = document.createElement("button");
    button.className = "text-button";
    button.textContent = "编辑";
    button.onclick = () => editAccountForm(account);
    control.insertAdjacentElement("beforebegin", button);
    const profile = document.createElement("button");
    profile.className = "text-button";
    profile.textContent = "人设";
    profile.onclick = () =>
      profileForm(account.id).catch((error) => toast(error.message, true));
    control.insertAdjacentElement("beforebegin", profile);
    if (account.platform === "telegram") {
      const reads = document.createElement("button");
      reads.className = "text-button";
      reads.textContent = "读取设置";
      reads.onclick = () => {
        if (reads.disabled) return;
        busy(reads, () => telegramReadSettings(account));
      };
      control.insertAdjacentElement("beforebegin", reads);
    }
    if (account.platform === "tiktok" && account.mode === "live") {
      const content = document.createElement("button");
      content.className = "text-button";
      content.textContent = "内容与评论";
      content.onclick = () =>
        tiktokLibrary(account.id).catch((e) => toast(e.message, true));
      control.insertAdjacentElement("beforebegin", content);
    }
  }
  document.querySelectorAll("[data-check]").forEach(
    (b) =>
      (b.onclick = () =>
        busy(b, async () => {
          const r = await api(`/accounts/${b.dataset.check}/check`, "POST");
          await refresh();
          accountRows(mode);
          toast(
            (r.connection_state || r.status) === "healthy"
              ? r.health.probe_cached
                ? "显示最近核验结果；发送前仍重新核验"
                : "账号身份与连接已核验"
              : "服务或账号尚未通过核验：" + (r.health.reason || "unknown"),
            (r.connection_state || r.status) !== "healthy",
          );
        })),
  );
  document.querySelectorAll("[data-disable]").forEach(
    (b) =>
      (b.onclick = () =>
        busy(b, async () => {
          await api(`/accounts/${b.dataset.disable}/enabled`, "POST", {
            enabled: b.dataset.value !== "true",
          });
          await refresh();
          accountRows(mode);
        })),
  );
}
async function telegramReadSettings(account) {
  const generation = openPanel(
    "Telegram 读取设置",
    '<p class="help">正在读取本机设置…</p>',
  );
  const isCurrent = () => generation === panelGeneration && $("#panel").open;
  let settings;
  try {
    settings = await api(`/accounts/${account.id}/telegram-reads`);
  } catch (error) {
    if (isCurrent()) toast(error.message, true);
    return;
  }
  if (!isCurrent()) return;
  const fields = [
    ["requests_per_window", "窗口读取预算", 1, 10000],
    ["window_seconds", "预算窗口（秒）", 60, 86400],
    ["scan_rpc_limit", "每轮扫描读取上限", 1, 1000],
    ["messages_per_page", "每页消息数", 1, 100],
    ["groups_per_scan", "每轮扫描群数", 1, 100],
    ["cache_ttl_seconds", "内容缓存有效期（秒）", 0, 3600],
  ];
  $("#panel-content").innerHTML =
    '<p class="help">此处设置本机内置执行器预算；旧外部 MCP 通道未覆盖；云端执行器需独立配置并独立统计，不跨设备合计限额。设置仅适用于此 Telegram 账号的本机读取，不改变已冻结的任务清单。读取预算不代表平台许可，也不保证不会受到平台限流。预算按本机记录的读取调用计数；消息条数不等于调用数，也不代表底层网络请求总数。必要身份与权限核验、账号冷却仍然生效。</p><form id="telegram-read-form"><div class="field-grid">' +
    fields
      .map(
        ([key, label, min, max]) =>
          `<div><label for="telegram-read-${key}">${label}</label><input id="telegram-read-${key}" name="${key}" type="number" min="${min}" max="${max}" step="1" value="${esc(settings.policy[key])}" required></div>`,
      )
      .join("") +
    '</div><p class="help">缓存仅用于消息内容读取，不替代发言权限检查。保存仅更新本机设置，不检查连接、不登录、不启动任务。</p><p class="help">关闭窗口仅关闭表单；已提交的保存请求仍可能完成。</p><div class="form-error" role="alert"></div><div class="form-actions"><button type="submit">保存读取设置</button></div></form>';
  $("#telegram-read-form input")?.focus();
  let pending = false;
  $("#telegram-read-form").onsubmit = (event) => {
    event.preventDefault();
    if (pending || !isCurrent()) return;
    pending = true;
    const form = new FormData(event.target);
    const policy = Object.fromEntries(
      fields.map(([key]) => [key, Number(form.get(key))]),
    );
    $("#panel-content .form-error").textContent = "";
    busy(
      event.submitter,
      async () => {
        await api(`/accounts/${account.id}/telegram-reads`, "PUT", policy, {
          "If-Match": String(settings.revision),
        });
        if (!isCurrent()) return;
        closePanel();
        toast("Telegram 本机读取设置已保存");
      },
      isCurrent,
    ).finally(() => {
      pending = false;
    });
  };
}
function outboundBlockReason(reason) {
  return (
    {
      x_challenge: "X 需要人工验证",
      x_login_required: "X 登录需要人工检查",
      x_account_restricted: "X 账号受到限制",
      telegram_speaking_restricted: "Telegram 连续发言被拒绝，需确认限制范围",
      browser_page_anomaly: "页面连续异常，暂时停止外发",
      account_guard_unavailable: "本地阻断证据待恢复，外发已暂停",
    }[reason] || reason
  );
}
function accountBlockForm(account, mode) {
  let block = account.outbound_block;
  if (!block) return;
  const generation = openPanel(
    "处理账号外发暂停",
    '<p id="outbound-block-reason"></p><p id="outbound-block-time" class="help"></p><p>受影响任务</p><ul id="outbound-block-tasks"></ul><p class="help">请先在平台上人工检查账号与限制范围，再运行只读检查。检查通过后，单独点击恢复后续外发。恢复不会重排结果未知、已尝试或已过期的任务，也不会切换发布通道。</p><p class="help">关闭窗口不会恢复外发；已经提交的检查或恢复请求仍可能完成。</p><p id="outbound-block-health" role="status">尚未执行恢复检查</p><div class="form-error" role="alert"></div><div class="form-actions"><button id="check-outbound-block" class="secondary">只读检查身份与页面</button><button id="resume-outbound-block" disabled>恢复后续外发</button></div>',
  );
  const isCurrent = () => generation === panelGeneration && $("#panel").open;
  const checkButton = $("#check-outbound-block");
  const resumeButton = $("#resume-outbound-block");
  const health = $("#outbound-block-health");
  let pending = false;
  function details() {
    $("#outbound-block-reason").textContent = outboundBlockReason(block.reason);
    $("#outbound-block-time").textContent =
      "暂停时间：" +
      fmt(block.since) +
      (block.until ? "；暂停到期时间：" + fmt(block.until) : "");
    const tasks = $("#outbound-block-tasks");
    tasks.replaceChildren();
    for (const task of block.affected_tasks || []) {
      const li = document.createElement("li");
      li.textContent = typeof task === "object" ? task.name || task.id : task;
      tasks.append(li);
    }
    if (!tasks.childElementCount) {
      const li = document.createElement("li");
      li.textContent = "该账号的后续外发任务";
      tasks.append(li);
    }
  }
  function updateAccount(updated) {
    state.accounts = state.accounts.map((a) =>
      a.id === updated.id ? updated : a,
    );
    if (location.hash.startsWith("#accounts") && $("#account-list"))
      accountRows(mode);
  }
  details();
  checkButton.onclick = () => {
    if (pending || !isCurrent()) return;
    pending = true;
    resumeButton.disabled = true;
    $("#panel-content .form-error").textContent = "";
    busy(
      checkButton,
      async () => {
        const updated = await api(
          `/accounts/${account.id}/outbound-block/check`,
          "POST",
        );
        if (!isCurrent()) return;
        updateAccount(updated);
        block = updated.outbound_block;
        if (!block) {
          closePanel();
          toast("账号暂停状态已变化，请重新检查账号列表");
          return;
        }
        details();
        health.textContent = block.health_ok
          ? "只读检查通过：" +
            fmt(block.health_checked_at) +
            "。可显式恢复后续外发。"
          : "只读检查未通过，账号外发仍暂停。请人工检查后重新验证。";
        resumeButton.disabled = !block.health_ok;
      },
      isCurrent,
    ).finally(() => {
      pending = false;
    });
  };
  resumeButton.onclick = () => {
    if (pending || resumeButton.disabled || !isCurrent()) return;
    pending = true;
    checkButton.disabled = true;
    $("#panel-content .form-error").textContent = "";
    busy(
      resumeButton,
      async () => {
        try {
          const updated = await api(
            `/accounts/${account.id}/outbound-block/resume`,
            "POST",
            { generation: block.generation },
          );
          if (!isCurrent()) return;
          updateAccount(updated);
          closePanel();
          toast("账号后续外发已恢复；结果未知、已尝试及已过期任务保持原状态");
        } catch (error) {
          if (isCurrent()) {
            health.textContent = "恢复未完成，请重新运行只读检查。";
            block = { ...block, health_ok: false };
          }
          throw error;
        }
      },
      isCurrent,
    ).finally(() => {
      pending = false;
      if (isCurrent()) {
        checkButton.disabled = false;
        resumeButton.disabled = !block.health_ok;
      }
    });
  };
}
function editAccountForm(account) {
  const generation = openPanel(
    "编辑账号",
    `<form id="edit-account-form"><p class="help">平台与本机账号标签保持不变。已被未结束任务使用的账号需先结束旧任务。</p><div class="field-grid"><div class="full"><label>显示名称<input name="label" value="${esc(account.label)}" required maxlength="80"></label></div><div><label>每日限额<input name="daily_limit" type="number" value="${account.daily_limit}" min="1" max="300" required></label></div><div><label>每小时限额<input name="hourly_limit" type="number" value="${account.hourly_limit}" min="1" max="20" required></label></div><div><label>最小间隔（秒）<input name="min_interval" type="number" value="${account.min_interval}" min="${account.mode === "live" ? 120 : 0}" max="86400" required></label></div></div><div class="form-actions"><button type="submit">保存账号</button></div></form>`,
  );
  $("#edit-account-form").onsubmit = (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    busy(event.submitter, async () => {
      await api(`/accounts/${account.id}`, "PATCH", {
        label: form.get("label"),
        daily_limit: Number(form.get("daily_limit")),
        hourly_limit: Number(form.get("hourly_limit")),
        min_interval: Number(form.get("min_interval")),
      });
      if (generation !== panelGeneration || !$("#panel").open) return;
      closePanel();
      const closedGeneration = panelGeneration;
      await refresh();
      if (closedGeneration !== panelGeneration) return;
      renderAccounts();
    });
  };
}
function accountForm() {
  const generation = openPanel(
    "添加平台账号",
    `<form id="account-form"><div class="field-grid"><div><label for="platform">平台</label><select id="platform" name="platform">${Object.entries(
      names,
    )
      .map(([k, v]) => `<option value="${k}">${v}</option>`)
      .join(
        "",
      )}</select></div><div><label for="mode">连接方式</label><select name="mode" id="mode"><option value="live">本机真实账号</option><option value="simulation">模拟账号 · 无外部操作</option></select></div><div><label for="label">显示名称</label><input name="label" id="label" required placeholder="例如：品牌运营账号"></div><div><label for="backend-label">平台账号标签</label><input name="backend_label" id="backend-label" required placeholder="例如：brand-telegram"><span class="help">先在本机完成账号登录，再填写登录时使用的相同标签。</span></div><div><label for="daily">每日限额</label><input name="daily_limit" id="daily" type="number" value="5" min="1" max="300" required></div><div><label for="interval">最小间隔（秒）</label><input name="min_interval" id="interval" type="number" value="120" min="0" required></div></div><div class="form-error" id="account-error" role="alert"></div><div class="form-actions"><button type="submit">保存账号</button></div></form>`,
  );
  $("#account-form").onsubmit = (e) => {
    e.preventDefault();
    const form = new FormData(e.target);
    busy(e.submitter, async () => {
      const r = await api("/accounts", "POST", {
        platform: form.get("platform"),
        label: form.get("label"),
        backend_label: form.get("backend_label"),
        mode: form.get("mode"),
        daily_limit: Number(form.get("daily_limit")),
        min_interval: Number(form.get("min_interval")),
        hourly_limit: 3,
      });
      if (generation !== panelGeneration || !$("#panel").open) return;
      closePanel();
      const closedGeneration = panelGeneration;
      await refresh();
      if (closedGeneration !== panelGeneration) return;
      renderAccounts();
      toast("账号已登记，可开始连接检查");
    });
  };
}
function renderTasks() {
  const active = state.tasks.filter((t) => t.state === "active").length;
  $("#main").innerHTML =
    heading(
      "任务中心",
      "从发现到发布，把任务、排期与每一项结果放在一起。",
      '<button id="new-task">创建任务</button>',
    ) +
    `<div class="stats"><span class="stat"><strong>${state.tasks.length}</strong>全部任务</span><span class="stat"><strong>${active}</strong>进行中</span><span class="stat"><strong>${state.tasks.filter((t) => t.phase === "review").length}</strong>等待开始发送</span></div>${state.overview.global_stop ? '<div class="banner warning">任务执行当前暂停。可以继续创建和编辑任务；恢复后才会执行已启动的项目。</div>' : ""}<div class="toolbar"><div class="filter-tabs"><button class="selected" id="current-tasks">当前项目</button><button id="legacy-tasks">历史任务 · 只读</button></div><span class="spacer"></span><input id="task-search" aria-label="搜索任务" placeholder="搜索任务名称或 ID"></div><div class="surface" id="task-list"></div>`;
  $("#new-task").onclick = taskForm;
  $("#current-tasks").onclick = () => renderTasks();
  $("#legacy-tasks").onclick = (e) =>
    busy(e.target, async () => {
      const rows = await api("/legacy/tasks");
      $("#task-list").innerHTML = rows.length
        ? `<div class="section-header"><h2>现有平台任务</h2><span class="badge">原平台负责执行</span></div><div class="table-wrap" role="region" aria-label="数据表格，可横向滚动" tabindex="0"><table><thead><tr><th>任务</th><th>平台</th><th>状态</th><th>来源</th></tr></thead><tbody>${rows.map((t) => `<tr><td>${esc(t.name)}<span class="sub">${esc(t.task_id)}</span></td><td>${platform(t.platform)}</td><td>${esc(t.status)}</td><td><code>${esc(t.source)}</code></td></tr>`).join("")}</tbody></table></div>`
        : empty("未找到可读取的历史任务", "历史任务由原系统继续负责执行。");
    });
  $("#task-search").oninput = taskRows;
  taskRows();
}
function taskRows() {
  const query = $("#task-search").value.toLowerCase();
  const rows = state.tasks.filter((t) =>
    (t.name + t.id).toLowerCase().includes(query),
  );
  $("#task-list").innerHTML = rows.length
    ? `<div class="table-wrap" role="region" aria-label="数据表格，可横向滚动" tabindex="0"><table><thead><tr><th>任务</th><th>平台</th><th>当前阶段</th><th>状态</th><th>创建时间</th></tr></thead><tbody>${rows
        .map(
          (t) =>
            `<tr class="task-row"><td><a class="task-link" href="#tasks/${t.id}">${esc(t.name)}</a><span class="sub">${esc(t.id)}${t.mode === "simulation" ? " · 模拟任务" : ""}</span></td><td><div class="chips">${[...new Set(t.account_snapshot.map((a) => a.platform))].map((p) => `<span class="chip">${names[p]}</span>`).join("")}</div></td><td>${phases[t.phase] || t.phase}<span class="sub">${
              Object.entries(t.summary)
                .map(([s, n]) => `${statuses[s] || s} ${n}`)
                .join(" · ") || "尚无执行项"
            }</span></td><td>${taskExecutionBadge(t)}</td><td class="nowrap">${fmt(t.created_at)}</td></tr>`,
        )
        .join("")}</tbody></table></div>`
    : empty(
        "开始第一个推广任务",
        "选择账号，定义发现范围，再准备清单并点击开始发送。",
        '<button id="empty-new-task" class="secondary">创建任务</button>',
      );
  $("#empty-new-task")?.addEventListener("click", taskForm);
}
function taskForm() {
  if (!state.accounts.length) {
    toast("请先在账号中心添加账号");
    location.hash = "accounts";
    return;
  }
  const now = Date.now();
  const generation = openPanel(
    "创建推广任务",
    `<form id="task-form"><div class="field-grid"><div class="full"><label for="task-name">任务名称</label><input id="task-name" name="name" required placeholder="例如：AI 视频创作者互动 · 第一批"></div><fieldset class="full"><legend>选择执行账号</legend>${state.accounts.map((a) => `<label class="checkbox-label"><input type="checkbox" name="accounts" value="${a.id}">${esc(a.label)} · ${names[a.platform]} · ${a.mode === "simulation" ? "模拟" : "真实"}</label>`).join("")}</fieldset><div class="full"><label for="queries">发现主题（每行一个）</label><textarea id="queries" name="queries" placeholder="AI video creation" rows="2"></textarea></div><div><label for="required">必需关键词（逗号分隔）</label><input id="required" name="required" placeholder="AI, video"></div><div><label for="blocked">排除关键词（逗号分隔）</label><input id="blocked" name="blocked" placeholder="airdrop, giveaway"></div><div><label for="starts">执行开始时间</label><input id="starts" name="starts" type="datetime-local" value="${isoLocal(now)}" required></div><div><label for="ends">执行结束时间</label><input id="ends" name="ends" type="datetime-local" value="${isoLocal(now + 86400000)}" required></div><div><label for="languages">允许语言</label><input id="languages" name="languages" value="en, zh" required></div><div><label for="candidates">最多候选数</label><input id="candidates" name="candidates" type="number" min="1" max="200" value="20" required></div><div><label for="task-daily">每日外发限额</label><input id="task-daily" name="daily" type="number" value="5" min="1" max="100" required></div><div><label for="task-interval">最小间隔（秒）</label><input id="task-interval" name="interval" type="number" value="120" min="0" required></div></div><details><summary>更多发现边界</summary><div class="field-grid"><div><label>最低成员数<input name="min_members" type="number" value="0" min="0"></label></div><div><label>最低活跃度<input name="min_activity" type="number" value="0" min="0"></label></div><div><label>最低置信度<input name="confidence" type="number" value="0.8" min="0.5" max="1" step="0.05"></label></div><div><label>最多 Telegram 入群数<input name="max_joins" type="number" value="0" min="0" max="20"></label></div><div class="full"><label>目标白名单（每行一个，空白表示使用发现政策）<textarea name="targets" rows="2"></textarea></label></div></div></details><p class="help">仅公开目标。输入时间使用浏览器本地时区，保存时转换为明确的 UTC 时间；任务详情统一显示北京时间。</p><div class="form-actions"><button type="submit">创建任务</button></div></form>`,
  );
  $("#task-form fieldset").insertAdjacentHTML(
    "afterend",
    '<div class="full"><label for="ordering-mode">多账号执行方式</label><select id="ordering-mode" name="ordering_mode"><option value="independent_accounts">各账号独立执行</option><option value="strict">严格按清单顺序</option></select><span class="help">独立执行时，一个账号冷却不会挡住其他账号；同一账号仍按清单顺序执行。</span></div>',
  );
  $("#task-form fieldset").insertAdjacentHTML(
    "afterend",
    '<div class="full"><label for="execution-channel">执行通道</label><select id="execution-channel" name="execution_channel"><option value="inherit">跟随默认</option><option value="cloud">在线队列</option><option value="local">本地队列</option></select><span class="help">只决定本次任务的通道；同一账号仍按顺序执行。在线连接失败时等待恢复。</span></div>',
  );
  $("#task-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const split = (n) =>
      String(f.get(n) || "")
        .split(/[,，\n]/)
        .map((s) => s.trim())
        .filter(Boolean);
    busy(e.submitter, async () => {
      const task = await api("/tasks", "POST", {
        name: f.get("name"),
        account_ids: f.getAll("accounts"),
        policy: {
          execution_channel: f.get("execution_channel"),
          ...(f.get("allow_owned_x_posts") === "on"
            ? { allow_owned_x_posts: true }
            : {}),
          ...(split("outbound_after_task_ids").length
            ? {
                outbound_after_task_ids: split("outbound_after_task_ids"),
                outbound_after_tasks_seconds:
                  Number(f.get("outbound_after_tasks_minutes")) * 60,
              }
            : {}),
          queries: split("queries"),
          required_keywords: split("required"),
          blocked_keywords: split("blocked"),
          languages: split("languages"),
          public_only: true,
          min_members: Number(f.get("min_members")),
          min_activity: Number(f.get("min_activity")),
          min_confidence: Number(f.get("confidence")),
          max_candidates: Number(f.get("candidates")),
          max_joins: Number(f.get("max_joins")),
          timezone: "Asia/Shanghai",
          starts_at: new Date(f.get("starts")).toISOString(),
          ends_at: new Date(f.get("ends")).toISOString(),
          min_interval: Number(f.get("interval")),
          ordering_mode: f.get("ordering_mode"),
          daily_limit: Number(f.get("daily")),
          target_allowlist: split("targets"),
        },
      });
      if (generation !== panelGeneration || !$("#panel").open) return;
      closePanel();
      const closedGeneration = panelGeneration;
      await refresh();
      if (closedGeneration !== panelGeneration) return;
      location.hash = "tasks/" + task.id;
    });
  };
  $("#task-form .form-actions").insertAdjacentHTML(
    "beforebegin",
    '<fieldset><legend>X 原创发布</legend><label class="checkbox-label"><input type="checkbox" name="allow_owned_x_posts">允许本任务向自有 X 账号发布原创推文</label><p class="help">使用在线队列，保留账号限额和每条仅提交一次的规则。每个账号分别创建一条发布任务。</p><label>等待完成的互动任务 ID（每行一个，可留空）<textarea name="outbound_after_task_ids" rows="2"></textarea></label><label>全部核验发布后等待（分钟）<input type="number" name="outbound_after_tasks_minutes" min="0" max="10080" value="60"></label><p class="help">有漏发、结果未知或缺少实际发布时间时继续等待。目标时间已过时不会补发。</p></fieldset>',
  );
  $("#task-form [name=allow_owned_x_posts]").onchange = (e) => {
    if (e.target.checked) $("#execution-channel").value = "cloud";
  };
}
function taskReadinessPanel(t) {
  if (["completed", "cancelled"].includes(t.state)) return "";
  const checks = t.readiness;
  if (!checks) return "";
  const messages = [
    ...checks.blockers.map((b) => b.message),
    ...checks.items.map((b) => {
      const item = t.items.find((i) => i.id === b.item_id);
      const owner = [
        b.blocked_by_task_id ? "占用任务 " + b.blocked_by_task_id : "",
        b.blocked_by_item_id ? "占用项目 " + b.blocked_by_item_id : "",
      ]
        .filter(Boolean)
        .join(" · ");
      return `${item?.payload.target || "目标"}：${b.message}${owner ? "（" + owner + "）" : ""}`;
    }),
  ];
  const skips = [...t.items, ...t.candidates]
    .filter((i) => i.result?.skip_message)
    .map((i) => `${i.payload.target}：${i.result.skip_message}`);
  if (!messages.length && !skips.length) return "";
  return `<div class="surface padded mt-20" id="task-readiness"><h2>执行前检查</h2>${
    messages.length
      ? `<ul>${[...new Set(messages)].map((m) => `<li>${esc(m)}</li>`).join("")}</ul>`
      : ""
  }${skips.length ? `<details><summary>最近一次跳过执行的原因</summary><ul>${skips.map((m) => `<li>${esc(m)}</li>`).join("")}</ul></details>` : ""}</div>`;
}

function taskChannelPanel(t) {
  const channel = t.channel;
  if (!channel) return "";
  const labels = { inherit: "跟随默认", cloud: "在线队列", local: "本地队列" };
  const reasons = {
    NotFound: "在线服务尚未升级以支持任务通道选择",
    CloudTransportUnavailable: "在线连接暂不可用，等待恢复",
    CloudControlConfigurationMissing: "尚未连接在线队列",
    LocalChannelReceiptPending: "等待该账号的回执保存完成",
    TaskChannelHandoffBusy: "等待该账号当前操作完成",
    AccountChannelHandoffBusy: "等待该账号在线操作完成",
    TaskChannelHandoffPending: "等待该账号完成通道交接",
    AccountChannelGenerationConflict: "通道状态已更新，等待核对",
  };
  const accounts = channel.accounts
    .map((a) => {
      const label =
        t.account_snapshot.find((x) => x.id === a.account_id)?.label || "账号";
      const desired =
        channel.requested === "inherit" ? channel.default : channel.requested;
      const actual =
        a.state !== "ready"
          ? "等待交接"
          : a.channel === desired
            ? labels[a.channel]
            : `等待交接到${labels[desired]}（账号当前：${labels[a.channel]}）`;
      const reason = a.error ? reasons[a.error] || "等待队列核验" : "";
      return `<li>${esc(label)}：${esc(actual)}${reason ? " · " + esc(reason) : ""}</li>`;
    })
    .join("");
  return `<div class="surface padded mt-20" id="task-channel"><h2>执行通道</h2><p>任务选择：${esc(labels[channel.requested] || "跟随默认")}${channel.requested === "inherit" ? "（当前默认：" + esc(labels[channel.default]) + "）" : ""}</p>${accounts ? "<ul>" + accounts + "</ul>" : '<p class="help">开始任务后确定账号的实际通道。</p>'}<p class="help">同一账号等待当前操作和回执完成后交接，其他账号独立执行。在线任务遇到连接问题时保持等待。</p></div>`;
}

const taskStartOperations = new Map();
let taskRenderGeneration = 0;
function startTaskFromUI(id, button) {
  if (taskStartOperations.has(id)) return taskStartOperations.get(id);
  const generation = panelGeneration;
  const isCurrent = () =>
    location.hash === "#tasks/" + id &&
    generation === panelGeneration &&
    !$("#panel").open;
  const operation = busy(
    button,
    async () => {
      const result = await api(`/tasks/${id}/start`, "POST");
      if (!isCurrent()) return;
      await refresh();
      if (!isCurrent()) return;
      const current = await renderTask(id);
      if (!current || !isCurrent()) return;
      const processed = result.execution_result?.processed || [];
      toast(
        processed.length
          ? `本轮处理 ${processed.length} 项；${current.readiness.workflow.message}`
          : current.readiness.workflow.message,
      );
    },
    isCurrent,
  ).finally(() => {
    taskStartOperations.delete(id);
    const control = $("#run-task");
    if (location.hash === "#tasks/" + id && control) control.disabled = false;
  });
  taskStartOperations.set(id, operation);
  return operation;
}
async function renderTask(id) {
  const generation = ++taskRenderGeneration;
  const isCurrent = () =>
    generation === taskRenderGeneration &&
    location.hash === "#tasks/" + id &&
    !$("#panel").open;
  const t = await api("/tasks/" + id);
  if (!isCurrent()) return;
  state.current = t;
  const discovery = true;
  const started = ["starting", "ready"].includes(t.phase);
  const ended = ["cancelled", "completed"].includes(t.state);
  $("#main").innerHTML =
    `<div class="detail-top"><a href="#tasks">任务中心</a><span class="muted">/</span><code>${esc(t.id)}</code>${t.mode === "simulation" ? '<span class="badge amber">模拟任务 · 无外部发布</span>' : ""}</div>` +
    heading(
      esc(t.name),
      "计划时间以北京时间显示；逐项保存执行结果和核验记录。",
      `<div class="actions"><span id="task-state">${taskExecutionBadge(t)}</span><button class="secondary compact" id="refresh-task">刷新</button></div>`,
    ) +
    `<div id="task-execution">${taskExecutionPanel(t)}</div>` +
    taskChannelPanel(t) +
    taskReadinessPanel(t) +
    `<div class="steps"><span class="${discovery ? "done" : ""}"><b>1</b>发现政策</span><span class="${t.manifest ? "done" : ""}"><b>2</b>准备清单</span><span class="${started ? "done" : ""}"><b>3</b>开始发送</span><span class="${t.state === "completed" ? "done" : ""}"><b>4</b>批次结果</span></div><div class="detail-grid"><div class="detail-body"><div class="surface"><div class="section-header"><h2>执行清单 <span class="muted">${t.items.length}</span></h2>${!t.manifest && !started && !ended ? '<button id="edit-items" class="secondary compact">编辑执行项</button>' : ""}</div>${t.items.length ? t.items.map((item) => itemPreview(item)).join("") : empty("尚无执行项", "添加每项的目标、内容、媒体和时间，然后点击开始发送。")}</div><div class="surface mt-24"><div class="section-header"><h2>发现候选 <span class="muted">${t.candidates.length}</span></h2></div><div class="padded">${
      discovery && !ended
        ? `<div class="actions">${t.account_snapshot
            .filter((a) =>
              state.accounts
                .find((x) => x.id === a.id)
                ?.capabilities.includes("discover"),
            )
            .map(
              (a) =>
                `<button class="secondary compact" data-discover="${a.id}">搜索 ${names[a.platform]}</button>`,
            )
            .join(
              "",
            )}<button class="secondary compact" id="add-candidate">录入候选</button>${t.policy.max_joins ? '<button class="secondary compact" id="join-candidates">开始入群</button>' : ""}</div>`
        : ""
    }${t.candidates.length ? '<div class="table-wrap" role="region" aria-label="数据表格，可横向滚动" tabindex="0"><table><thead><tr><th>候选目标</th><th>资格判断</th><th>原因</th></tr></thead><tbody>' + t.candidates.map((c) => `<tr><td>${esc(c.target)}</td><td>${badge(c.decision)}</td><td>${esc(c.reasons.join(" · ") || "通过发现政策")}<span class="sub">${esc(c.state)}</span>${["unknown", "pending_admin_approval", "joined", "already_joined"].includes(c.state) ? `<button class="text-button" data-join-reconcile="${c.id}">只读核对入群</button>` : ""}</td></tr>`).join("") + "</tbody></table></div>" : '<p class="muted mt-14">搜索结果先经过资格筛选，低置信度候选会被记录为拒绝。</p>'}</div></div><div class="surface mt-24"><div class="section-header"><h2>任务日志</h2></div><div class="padded" id="task-audit">正在读取…</div></div></div><aside class="detail-aside"><div class="surface padded"><h2>任务范围</h2><details><summary>查看完整范围</summary><p class="help">账号：${t.account_snapshot.map((a) => esc(a.label) + " (" + names[a.platform] + ")").join("、")}</p><pre>${esc(JSON.stringify(t.policy, null, 2))}</pre></details>${t.manifest ? `<details><summary>查看发送清单</summary><pre>${esc(JSON.stringify(t.manifest, null, 2))}</pre></details>` : '<p class="help">点击开始发送时自动固定清单，无需审批或确认短语。</p>'}</div><div class="surface padded mt-20"><h2>执行控制</h2><p class="help">${state.overview.global_stop ? "全局执行已暂停。请使用顶部开关恢复。" : "任务启动后按计划时间执行。"}</p><div class="actions mt-14">${!ended ? `<button id="run-task" class="compact" ${taskStartOperations.has(id) ? "disabled" : ""}>${t.items.length ? "开始发送" : t.policy.max_joins ? "开始入群" : "开始任务"}</button><button class="secondary compact" id="pause-task">${t.state === "paused" ? "恢复任务" : "暂停任务"}</button><button class="danger compact" id="cancel-task">取消任务</button>` : ""}</div><p class="native-note">停止本地任务不会撤回已经提交给 Telegram 的原生排期。</p></div></aside></div>`;
  bindTaskTools(t);
  $("#refresh-task").onclick = () => renderTask(id);
  $("#edit-items")?.addEventListener("click", () => itemEditor(t));
  $("#run-task")?.addEventListener("click", (e) =>
    startTaskFromUI(id, e.currentTarget),
  );
  $("#pause-task")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      await api(`/tasks/${id}/state`, "POST", {
        state: t.state === "paused" ? "active" : "paused",
      });
      await refresh();
      await renderTask(id);
    }),
  );
  $("#cancel-task")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      await api(`/tasks/${id}/state`, "POST", { state: "cancelled" });
      await refresh();
      await renderTask(id);
    }),
  );
  document.querySelectorAll("[data-reconcile]").forEach(
    (b) =>
      (b.onclick = () =>
        busy(b, async () => {
          const r = await api(
            `/items/${b.dataset.reconcile}/reconcile`,
            "POST",
          );
          if (!(await renderTask(id))) return;
          toast(
            r.verified ? "只读核验完成" : "尚未取得可确认结果；不会自动重发",
          );
        })),
  );
  document
    .querySelectorAll("[data-manual]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          manualForm(t.items.find((i) => i.id === b.dataset.manual))),
    );
  document.querySelectorAll("[data-discover]").forEach(
    (b) =>
      (b.onclick = () =>
        busy(b, async () => {
          await api(`/tasks/${id}/discover`, "POST", {
            account_id: b.dataset.discover,
          });
          if (await renderTask(id)) toast("候选已筛选并记录");
        })),
  );
  $("#join-candidates")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      await api(`/tasks/${id}/join`, "POST");
      if (await renderTask(id)) toast("合格候选入群批次已处理");
    }),
  );
  document.querySelectorAll("[data-join-reconcile]").forEach(
    (b) =>
      (b.onclick = () =>
        busy(b, async () => {
          await api(`/candidates/${b.dataset.joinReconcile}/reconcile`, "POST");
          if (await renderTask(id)) toast("已只读核对成员状态；不会重新入群");
        })),
  );
  $("#add-candidate")?.addEventListener("click", () => candidateForm(t));
  const logs = await api("/audit?task_id=" + encodeURIComponent(id));
  if (isCurrent() && state.current?.id === id && $("#task-audit"))
    $("#task-audit").innerHTML = logs.length
      ? logs
          .slice(0, 25)
          .map(
            (r) =>
              `<div class="log-row"><span>${esc(r.kind)}<br><code>${esc(JSON.stringify(r.details))}</code></span><span class="muted nowrap">${fmt(r.at)}</span></div>`,
          )
          .join("")
      : "暂无记录";
  return isCurrent() ? t : undefined;
}
function itemPreview(i) {
  const p = i.payload;
  return `<article class="item-preview"><div class="item-head"><strong>${i.position + 1}. ${esc(p.account_label)} · ${actions[p.action] || esc(p.action)}</strong>${badge(i.status)}</div><div class="item-meta"><span>目标：${esc(p.target)}</span>${p.discord_reply_to ? `<span>回复消息 ID：${esc(p.discord_reply_to)}</span>` : ""}<span>${fmt(p.scheduled_at)} UTC+08:00</span></div>${tiktokItemSummary(p, i.result)}${p.title ? `<h3>${esc(p.title)}</h3>` : ""}${p.text ? `<div class="item-text">${esc(p.text)}</div>` : ""}${p.media.length ? '<p class="help">媒体：' + p.media.map((m) => esc(m.filename) + " · " + esc(m.sha256.slice(0, 12))).join("，") + "</p>" : ""}${i.external_reference ? '<p class="help">回执：<code>' + esc(i.external_reference) + "</code></p>" : ""}${i.result.execution_step ? `<p class="help">执行步骤：${esc(executionSteps[i.result.execution_step] || i.result.execution_step)} · ${i.result.submission_started ? "已开始提交，需核对结果" : "尚未开始提交"}</p>` : ""}${i.result.diagnostic_traceback?.length ? `<details><summary>诊断详情</summary><pre>${esc(JSON.stringify(i.result.diagnostic_traceback, null, 2))}</pre></details>` : ""}${i.result.reason ? '<p class="help">' + esc(i.result.reason) + "</p>" : ""}<div class="actions">${["unknown", "scheduled"].includes(i.status) ? `<button class="text-button" data-reconcile="${i.id}">只读核对结果</button>` : ""}${i.status === "manual_attention" ? `<button class="text-button" data-manual="${i.id}">登记人工发布结果</button>` : ""}</div></article>`;
}

const executionSteps = {
  backend_start: "启动执行后端",
  platform_submission: "准备提交平台",
  browser_start: "启动浏览器",
  browser_ready: "浏览器就绪",
  home_navigation: "打开平台首页",
  identity_check: "核对账号身份",
  target_navigation: "打开目标帖子",
  target_lookup: "定位目标帖子",
  reply_control: "打开回复入口",
  reply_composer: "定位回复输入框",
  fill_reply: "填写回复",
  attach_media: "添加媒体",
  submission_authorization: "检查提交权限",
  submit_click: "点击发布",
  submission_response: "等待发布回执",
  parse_receipt: "读取发布回执",
  receipt_saved: "保存发布回执",
  receipt_navigation: "打开回复核验",
  receipt_lookup: "定位已发布回复",
  receipt_validation: "核验回复内容",
  completed: "核验完成",
};
function taskExecutionBadge(t) {
  if (t.state === "completed") {
    const d = t.delivery_summary;
    if (!d) return '<span class="badge amber">批次已结束 · 送达未验证</span>';
    return `<span class="badge ${d.unknown || d.missed || d.unverified ? "amber" : ""}">批次已结束 · 已确认 ${d.confirmed} · 模拟 ${d.simulated} · 过窗未发 ${d.missed} · 未知 ${d.unknown}</span>`;
  }
  const flow = t.readiness?.workflow;
  if (
    flow &&
    !["ready", "verifying", "completed", "cancelled"].includes(flow.status)
  )
    return `<span class="badge amber">${esc(flow.label)}</span>`;
  if (t.state !== "active" || t.phase !== "ready") return badge(t.state);
  const labels = {
    running: "发送进程运行中",
    stale: "发送心跳超时",
    stopped: "发送已停止",
    unmonitored: "已启动 · 待执行",
  };
  const status = t.execution?.status || "unmonitored";
  return `<span class="badge ${status === "running" ? "green" : "amber"}">${labels[status]}</span>`;
}
function taskExecutionPanel(t) {
  const flow = t.readiness?.workflow;
  const service = flow?.service_observation;
  const observation = service
    ? `<p>本任务同步观测：${service.task_scope_failed ? "当前同步失败（" + esc(service.task_state) + "）" : service.task_scope_usable ? "近期同步成功" : "尚无有效的近期同步证据"}${service.task_at ? " · " + fmt(service.task_at) : ""}。状态观测不替代提交前授权。</p>${service.task_scope_failed && service.task_retry_at ? `<p>本任务同步重试时间：${fmt(service.task_retry_at)} UTC+08:00</p>` : ""}${service.global_state && service.global_state !== "connected" ? `<p>${service.historical_conflict ? "全局历史同步仍有冲突，已与本任务近期同步分开显示" : "全局服务观测：" + esc(service.global_state)}${service.global_at ? " · " + fmt(service.global_at) : ""}</p>` : ""}`
    : "";
  const progress = flow?.counts;
  const delivery = t.delivery_summary;
  const outcome = delivery
    ? `<div class="banner" id="task-delivery"><div><strong>投递结果（批次结束不代表全部送达）</strong><p>真实已核验 ${delivery.confirmed} · 模拟 ${delivery.simulated} · 过窗未发送 ${delivery.missed} · 结果未知 ${delivery.unknown} · 明确失败 ${delivery.failed_known} · 证据未完整 ${delivery.unverified}</p><p>unknown 只读核验，禁止自动补发。</p></div></div>`
    : "";
  const workflow =
    outcome +
    (flow
      ? `<div class="banner" id="task-workflow"><div><strong>${esc(flow.label)}</strong><p>${esc(flow.message)}</p>${flow.next_at ? `<p>${flow.next_at_kind === "service_retry" ? "下次服务检查" : "下一计划时间"}：${fmt(flow.next_at)} UTC+08:00</p>` : ""}${flow.schedule_next_at && (flow.next_at_kind === "service_retry" || flow.schedule_next_at !== flow.next_at) ? `<p>原定下一计划时间：${fmt(flow.schedule_next_at)} UTC+08:00</p>` : ""}${observation}${progress ? `<p>已核验发布 ${progress.verified_sent} / ${progress.planned} · 未尝试 ${progress.not_attempted} · 已核验入群 ${progress.membership_verified}</p>` : ""}</div></div>`
      : "");
  if (!t.manifest) return workflow;
  const e = t.execution || { status: "unmonitored" };
  const messages = {
    running: "发送进程心跳正常，按已启动的窗口和限额执行。",
    stale: "超过 60 秒未收到发送心跳，无法确认进程仍在运行。请检查执行服务。",
    stopped: "发送进程已结束，剩余项目不会由该进程继续执行。",
    unmonitored: "尚无发送进程监测记录；任务状态不能证明发送正在运行。",
  };
  return (
    workflow +
    `<div class="banner ${e.status === "running" ? "" : "warning"}"><div><strong>后台进程状态</strong><p>${messages[e.status] || messages.unmonitored}</p>${e.last_heartbeat ? `<p>最后心跳：${fmt(e.last_heartbeat)} UTC+08:00</p>` : ""}${e.finished_at ? `<p>退出记录时间：${fmt(e.finished_at)} UTC+08:00</p>` : ""}${e.reason ? `<p>退出或监测原因：${esc(e.reason === "process_exit_reason_unavailable" ? "历史记录未保存退出原因" : e.reason)}${e.exit_code != null ? ` · 退出码 ${esc(e.exit_code)}` : ""}</p>` : ""}</div></div>`
  );
}

let executionPolling = false;
setInterval(async () => {
  if (
    executionPolling ||
    $("#panel")?.open ||
    !location.hash.startsWith("#tasks/")
  )
    return;
  executionPolling = true;
  const id = location.hash.slice(7);
  const renderGeneration = taskRenderGeneration;
  const viewGeneration = panelGeneration;
  const isCurrent = () =>
    location.hash === "#tasks/" + id &&
    renderGeneration === taskRenderGeneration &&
    viewGeneration === panelGeneration &&
    !$("#panel").open;
  try {
    const task = await api("/tasks/" + id);
    if (isCurrent() && $("#task-execution")) {
      const previous = state.current;
      const changed =
        previous?.phase !== task.phase ||
        previous?.readiness?.workflow?.can_prepare !==
          task.readiness?.workflow?.can_prepare ||
        JSON.stringify(previous?.candidates.map((c) => [c.state, c.result])) !==
          JSON.stringify(task.candidates.map((c) => [c.state, c.result])) ||
        JSON.stringify(previous?.items.map((i) => i.status)) !==
          JSON.stringify(task.items.map((i) => i.status));
      if (
        changed &&
        !document.activeElement?.matches("input, textarea, select")
      ) {
        await renderTask(id);
        return;
      }
      $("#task-execution").innerHTML = taskExecutionPanel(task);
      if ($("#task-channel"))
        $("#task-channel").outerHTML = taskChannelPanel(task);
      $("#task-state").innerHTML = taskExecutionBadge(task);
      if ($("#task-readiness"))
        $("#task-readiness").outerHTML = taskReadinessPanel(task);
      if ($("#prepare"))
        $("#prepare").disabled = !task.readiness.workflow.can_prepare;
    }
  } catch (_) {
    if (isCurrent() && $("#task-execution"))
      $("#task-execution").textContent =
        "执行状态暂时无法刷新，请检查本机服务。";
  } finally {
    executionPolling = false;
  }
}, 10000);
function itemEditor(t) {
  let items = t.items.map((i) => {
    const p = { ...i.payload };
    delete p.position;
    delete p.platform;
    delete p.account_label;
    return p;
  });
  function blank() {
    const a = t.account_snapshot[0];
    const live = state.accounts.find((x) => x.id === a.id);
    return {
      execution_mode:
        t.policy.allow_owned_x_posts && live.platform === "x"
          ? "automatic"
          : live.mode === "live" && ["x", "reddit"].includes(live.platform)
            ? "manual"
            : "automatic",
      account_id: a.id,
      action:
        t.policy.allow_owned_x_posts && live.platform === "x"
          ? "post"
          : live.platform === "tiktok"
            ? live.tiktok_transport === "tabbit"
              ? "comment"
              : "publish"
            : state.accounts
                .find((x) => x.id === a.id)
                .capabilities.find((c) => !["discover", "join"].includes(c)),
      target:
        t.policy.allow_owned_x_posts && live.platform === "x"
          ? live.owned_target_url || ""
          : live.platform === "tiktok"
            ? live.tiktok_transport === "tabbit"
              ? ""
              : live.owned_target_id || ""
            : "",
      text: "",
      title: "",
      media: [],
      scheduled_at: new Date(
        Math.max(Date.now() + 60000, new Date(t.policy.starts_at).getTime()),
      ).toISOString(),
      reply_to: null,
      discord_reply_to: "",
      recipient_id: "",
      target_owned:
        (live.platform === "tiktok" && live.tiktok_transport !== "tabbit") ||
        (live.platform === "x" && t.policy.allow_owned_x_posts),
      tiktok_video_id: "",
      tiktok_comment_id: "",
      tiktok_options: null,
    };
  }
  if (!items.length)
    items.push(
      t.reply_context
        ? { ...blank(), ...t.reply_context, action: "reply" }
        : blank(),
    );
  const generation = openPanel("编辑执行清单", "");
  function targetField(p, n) {
    if (
      state.accounts.find((a) => a.id === p.account_id).platform === "x" &&
      p.action === "post"
    )
      return `<label>当前 X 账号主页<input name="target" value="${esc(p.target)}" readonly required></label><p class="help">发布前会再次核对当前账号和主页身份。</p>`;
    if (
      state.accounts.find((a) => a.id === p.account_id).platform !== "discord"
    )
      return `<label>目标<input name="target" value="${esc(p.target)}" placeholder="群组 @username、帖子链接、社区名称或自有账号 ID" required></label>`;
    return `<label for="discord-channel-${n}">Discord 频道</label><select id="discord-channel-${n}" name="target" required><option value="">请读取并选择已授权频道</option>${p.target ? `<option selected value="${esc(p.target)}">频道 ${esc(p.target)}</option>` : ""}</select><button class="secondary compact" type="button" data-discord-targets="${n}">读取已授权频道</button><p class="help" data-target-note="${n}">仅列出服务器成员可见且 Bot 可发送的频道。计划时间由本机调度。</p>`;
  }
  function replyField(p) {
    if (state.accounts.find((a) => a.id === p.account_id).platform === "tiktok")
      return "";
    return state.accounts.find((a) => a.id === p.account_id).platform ===
      "discord"
      ? `<label>Discord 原消息 ID（回复时）<input name="discord_reply_to" type="text" inputmode="numeric" pattern="[1-9][0-9]{0,19}" value="${esc(p.discord_reply_to || "")}" placeholder="完整消息 ID"></label>`
      : `<label>Telegram 原消息 ID（回复时）<input name="reply_to" type="number" min="1" value="${p.reply_to || ""}"></label>`;
  }
  function draw() {
    const container = $("#panel-content");
    container.innerHTML = `<form id="items-form"><p class="help">保存后仍可编辑；冻结清单后，任何修改都需要新任务。</p>${items
      .map(
        (p, n) =>
          `<fieldset class="editor-row" data-index="${n}"><div class="item-head"><h3>执行项 ${n + 1}</h3><button class="text-button" type="button" data-remove="${n}">移除</button></div><div class="field-grid"><div><label>账号<select name="account_id">${t.account_snapshot.map((a) => `<option value="${a.id}" ${p.account_id === a.id ? "selected" : ""}>${esc(a.label)}</option>`).join("")}</select></label></div><div><label>动作<select name="action">${state.accounts
            .find((a) => a.id === p.account_id)
            .capabilities.filter((c) => !["discover", "join"].includes(c))
            .map(
              (c) =>
                `<option value="${c}" ${p.action === c ? "selected" : ""}>${actions[c]}</option>`,
            )
            .join(
              "",
            )}</select></label></div><div class="full"><label>执行方式<select name="execution_mode"><option value="automatic" ${p.execution_mode !== "manual" ? "selected" : ""}>自动执行</option><option value="manual" ${p.execution_mode === "manual" ? "selected" : ""}>人工执行与结果登记</option></select></label></div><div class="full">${targetField(p, n)}</div>${tiktokItemFields(p)}<div class="full"><label>标题（帖子需要）<input name="title" value="${esc(p.title)}"></label></div><div class="full"><label>完整正文 / Caption<textarea name="text">${esc(p.text)}</textarea></label></div><div><label>计划时间<input name="scheduled_at" type="datetime-local" value="${isoLocal(new Date(p.scheduled_at).getTime())}" required></label></div><div>${replyField(p)}</div><div class="full"><label class="checkbox-label"><input type="checkbox" name="target_owned" ${p.target_owned ? "checked" : ""}>这是自有发布目标（Instagram 与 TikTok API 必须勾选，Tabbit 视频留言按目标链接校验）</label></div><div class="full"><label>媒体文件<input type="file" data-upload="${n}"></label><p class="help">${p.media.map((m) => esc(m.filename)).join(" · ") || "尚未添加媒体"} · ${state.accounts.find((a) => a.id === p.account_id).platform === "discord" ? "单文件最多 10 MiB，合计最多 25 MiB" : "最大 50 MB"}</p>${p.media.map((m, j) => `<button type="button" class="text-button" data-remove-media="${n}:${j}">移除此媒体</button><label>媒体 ${j + 1} 的 HTTPS 地址（Instagram 与 TikTok 必需）<input data-source="${j}" value="${esc(m.source_url)}" placeholder="https://..."></label><label>媒体类型<select data-media-type="${j}"><option value="">按文件类型</option>${["IMAGE", "VIDEO", "REELS"].map((v) => `<option ${m.media_type === v ? "selected" : ""}>${v}</option>`).join("")}</select></label>`).join("")}</div></div></fieldset>`,
      )
      .join(
        "",
      )}<button class="secondary" type="button" id="append-item">添加执行项</button><div class="form-actions"><button type="submit">保存清单</button></div></form>`;
    container.querySelectorAll("[data-index]").forEach((row) => {
      const item = items[Number(row.dataset.index)];
      const account = state.accounts.find((a) => a.id === item.account_id);
      if (account.platform !== "x" || item.action !== "post") return;
      const title = row.querySelector("[name=title]");
      title.type = "hidden";
      title.value = "";
      title.closest("div").hidden = true;
      item.title = "";
      const reply = row.querySelector("[name=reply_to]");
      if (reply) reply.closest("div").hidden = true;
      row
        .querySelector("[name=scheduled_at]")
        .closest("div")
        .classList.add("full");
      const file = row.querySelector('input[type="file"]');
      file.accept = "image/png,image/jpeg";
      file.disabled = item.media.length >= 4;
      file.closest("div").querySelector("p.help").textContent =
        (item.media.map((m) => m.filename).join(" · ") || "尚未添加配图") +
        " · PNG/JPEG，最多 4 张";
      row
        .querySelectorAll("[data-source], [data-media-type]")
        .forEach((field) => {
          field.closest("label").hidden = true;
        });
      const owned = row.querySelector("[name=target_owned]").parentElement;
      owned.lastChild.textContent = "这是当前 X 账号的自有发布目标";
    });
    function collect() {
      container.querySelectorAll("[data-index]").forEach((row) => {
        const n = Number(row.dataset.index),
          p = items[n];
        for (const name of [
          "account_id",
          "action",
          "target",
          "title",
          "text",
          "execution_mode",
        ])
          p[name] = row.querySelector(`[name=${name}]`).value;
        p.scheduled_at = new Date(
          row.querySelector("[name=scheduled_at]").value,
        ).toISOString();
        p.reply_to =
          Number(row.querySelector("[name=reply_to]")?.value) || null;
        p.discord_reply_to =
          row.querySelector("[name=discord_reply_to]")?.value.trim() || "";
        p.target_owned = row.querySelector("[name=target_owned]").checked;
        collectTikTokFields(row, p);
        row
          .querySelectorAll("[data-source]")
          .forEach(
            (el) => (p.media[Number(el.dataset.source)].source_url = el.value),
          );
        row
          .querySelectorAll("[data-media-type]")
          .forEach(
            (el) =>
              (p.media[Number(el.dataset.mediaType)].media_type = el.value),
          );
      });
    }
    container.querySelectorAll("[name=account_id]").forEach(
      (el) =>
        (el.onchange = () => {
          collect();
          const index = Number(el.closest("[data-index]").dataset.index);
          items[index].action = state.accounts
            .find((a) => a.id === el.value)
            .capabilities.find((c) => !["discover", "join"].includes(c));
          const account = state.accounts.find((a) => a.id === el.value);
          items[index].execution_mode =
            account.mode === "live" &&
            ["x", "reddit"].includes(account.platform)
              ? "manual"
              : "automatic";
          items[index].target = "";
          items[index].reply_to = null;
          items[index].discord_reply_to = "";
          items[index].tiktok_options = null;
          items[index].tiktok_video_id = "";
          items[index].tiktok_comment_id = "";
          if (account.platform === "tiktok") {
            items[index].target =
              account.tiktok_transport === "tabbit"
                ? ""
                : account.owned_target_id || "";
            items[index].target_owned = account.tiktok_transport !== "tabbit";
            items[index].action =
              account.tiktok_transport === "tabbit" ? "comment" : "publish";
          } else if (account.platform === "x" && t.policy.allow_owned_x_posts) {
            items[index].action = "post";
            items[index].execution_mode = "automatic";
            items[index].target = account.owned_target_url || "";
            items[index].target_owned = true;
          }
          draw();
        }),
    );
    container.querySelectorAll("[name=action]").forEach(
      (el) =>
        (el.onchange = () => {
          collect();
          const index = Number(el.closest("[data-index]").dataset.index);
          const account = state.accounts.find(
            (a) => a.id === items[index].account_id,
          );
          if (account.platform === "x" && el.value === "post") {
            items[index].target = account.owned_target_url || "";
            items[index].target_owned = true;
          }
          draw();
        }),
    );
    container.querySelectorAll("[data-discord-targets]").forEach((button) => {
      button.onclick = () =>
        busy(button, async () => {
          collect();
          const n = Number(button.dataset.discordTargets),
            p = items[n];
          const accountId = p.account_id;
          const result = await api(`/accounts/${accountId}/targets`);
          if (
            generation !== panelGeneration ||
            !$("#panel").open ||
            !button.isConnected ||
            items[n] !== p ||
            p.account_id !== accountId
          )
            return;
          const targets = result.targets.filter(
            (c) =>
              !t.policy.target_allowlist.length ||
              t.policy.target_allowlist.includes(c.id),
          );
          const select = button
            .closest("[data-index]")
            .querySelector("[name=target]");
          select.innerHTML =
            '<option value="">请选择频道</option>' +
            targets
              .map(
                (c) =>
                  `<option value="${esc(c.id)}" ${c.id === p.target ? "selected" : ""}>${esc(c.guild_name)} / #${esc(c.name)}${c.can_attach ? "" : "（无附件权限）"}${c.can_reply ? "" : "（公告频道，不能回复）"}</option>`,
              )
              .join("");
          container.querySelector(`[data-target-note="${n}"]`).textContent =
            targets.length
              ? `已读取 ${targets.length} 个批准范围内频道。${result.truncated ? "目录未全部读取；请缩小服务器规模后重试。" : ""}`
              : "暂无可用频道。请确认管理员已安装 Bot 并授予查看、发送和历史权限，且频道在任务白名单内。";
        });
    });
    container.querySelectorAll("[data-remove]").forEach(
      (b) =>
        (b.onclick = () => {
          collect();
          items.splice(Number(b.dataset.remove), 1);
          draw();
        }),
    );
    $("#append-item").onclick = () => {
      collect();
      const p = blank();
      if (items.length)
        p.scheduled_at = new Date(
          new Date(items.at(-1).scheduled_at).getTime() +
            Math.max(t.policy.min_interval, 120) * 1000,
        ).toISOString();
      items.push(p);
      draw();
    };
    if (t.account_snapshot.length > 1) {
      $("#append-item").insertAdjacentHTML(
        "afterend",
        '<button class="secondary" type="button" id="rotate-accounts">同平台账号轮流分配</button><button class="secondary" type="button" id="match-accounts">按已筛选目标匹配账号</button>',
      );
      const eligible = (item) => {
        const current = state.accounts.find(
          (account) => account.id === item.account_id,
        );
        return t.account_snapshot.filter((snapshot) => {
          const account = state.accounts.find(
            (entry) => entry.id === snapshot.id,
          );
          return (
            account?.platform === current?.platform &&
            account.status === "healthy" &&
            !account.disabled &&
            account.capabilities.includes(item.action) &&
            (current.platform !== "tiktok" ||
              item.action === "publish" ||
              account.id === current.id)
          );
        });
      };
      $("#rotate-accounts").onclick = () => {
        collect();
        const cursors = new Map();
        let assigned = 0;
        for (const item of items) {
          const accounts = eligible(item);
          if (!accounts.length) continue;
          const key = `${accounts[0].platform}:${item.action}`;
          const index = cursors.get(key) || 0;
          item.account_id = accounts[index % accounts.length].id;
          const selectedAccount = state.accounts.find(
            (a) => a.id === item.account_id,
          );
          if (
            selectedAccount.platform === "tiktok" &&
            selectedAccount.tiktok_transport !== "tabbit"
          )
            item.target = selectedAccount.owned_target_id || item.target;
          cursors.set(key, index + 1);
          assigned += 1;
        }
        draw();
        toast(`已轮流分配 ${assigned} 个执行项，请核对账号与目标。`);
      };
      $("#match-accounts").onclick = () => {
        collect();
        const counts = new Map();
        let assigned = 0;
        for (const item of items) {
          const candidates = new Set(
            t.candidates
              .filter(
                (candidate) =>
                  candidate.decision === "qualified" &&
                  !["unknown", "pending_admin_approval", "executing"].includes(
                    candidate.state,
                  ) &&
                  candidate.target.toLowerCase() === item.target.toLowerCase(),
              )
              .map((candidate) => candidate.account_id),
          );
          const accounts = eligible(item).filter((account) =>
            candidates.has(account.id),
          );
          if (!accounts.length) continue;
          accounts.sort(
            (left, right) =>
              (counts.get(left.id) || 0) - (counts.get(right.id) || 0),
          );
          item.account_id = accounts[0].id;
          counts.set(item.account_id, (counts.get(item.account_id) || 0) + 1);
          assigned += 1;
        }
        draw();
        toast(`已按目标匹配 ${assigned} 个执行项；未匹配的项目保留原账号。`);
      };
    }
    container.querySelectorAll("[data-upload]").forEach(
      (el) =>
        (el.onchange = async () => {
          const f = el.files[0];
          if (!f) return;
          try {
            collect();
            const response = await fetch("/api/media", {
              method: "POST",
              headers: {
                "X-AP-Client": "local-ui",
                "X-Filename": encodeURIComponent(f.name),
                "Content-Type": f.type || "application/octet-stream",
              },
              body: f,
            });
            const m = await response.json();
            if (
              generation !== panelGeneration ||
              !$("#panel").open ||
              !el.isConnected
            )
              return;
            if (!response.ok) throw Error(m.error);
            const currentItem = items[Number(el.dataset.upload)];
            if (
              state.accounts.find((a) => a.id === currentItem.account_id)
                .platform === "tiktok"
            )
              m.media_type = m.mime_type.startsWith("image/")
                ? "IMAGE"
                : m.mime_type.startsWith("video/")
                  ? "VIDEO"
                  : "";
            currentItem.media.push(m);
            draw();
          } catch (e) {
            if (
              generation === panelGeneration &&
              $("#panel").open &&
              el.isConnected
            )
              toast(e.message, true);
          }
        }),
    );
    container.querySelectorAll("[data-remove-media]").forEach(
      (button) =>
        (button.onclick = () => {
          collect();
          const [n, j] = button.dataset.removeMedia.split(":").map(Number);
          items[n].media.splice(j, 1);
          draw();
        }),
    );
    $("#items-form").onsubmit = (e) => {
      e.preventDefault();
      busy(e.submitter, async () => {
        collect();
        await api(`/tasks/${t.id}/items`, "PUT", items);
        if (generation !== panelGeneration || !$("#panel").open) return;
        closePanel();
        await renderTask(t.id);
      });
    };
  }
  draw();
}
function manualForm(item) {
  const generation = openPanel(
    "登记人工发布结果",
    `<p class="help">先核对任务文本，再在平台手动发布。登记后不会自动重试。</p><div class="item-text">${esc(item.payload.text)}</div><form id="manual-form"><label>平台上实际发布的完整文本<textarea name="text" required></textarea></label><label>Instagram 公开链接<input name="url" type="url" required></label><label class="checkbox-label"><input name="verified" type="checkbox">已在平台页面核验发布结果</label><div class="form-actions"><button>保存结果</button></div></form>`,
  );
  $("#manual-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    busy(e.submitter, async () => {
      await api(`/items/${item.id}/manual-outcome`, "POST", {
        observed_text: f.get("text"),
        external_reference: f.get("url"),
        verified: f.has("verified"),
      });
      if (generation !== panelGeneration || !$("#panel").open) return;
      closePanel();
      await renderTask(item.task_id);
    });
  };
}
function candidateForm(t) {
  const generation = openPanel(
    "录入发现候选",
    `<form id="candidate-form"><div class="field-grid"><div><label>账号<select name="account_id">${t.account_snapshot.map((a) => `<option value="${a.id}">${esc(a.label)}</option>`).join("")}</select></label></div><div><label>目标<input name="target" required></label></div><div class="full"><label>标题<input name="title"></label></div><div class="full"><label>上下文<textarea name="text"></textarea></label></div><div><label>语言<input name="language" value="en" required></label></div><div><label>证据置信度<input name="confidence" type="number" value="0.8" min="0" max="1" step="0.05" required></label></div><div><label>成员数<input name="members" type="number" value="0" min="0"></label></div><div><label>活跃度<input name="activity" type="number" value="0" min="0"></label></div></div><div class="form-actions"><button>筛选并保存</button></div></form>`,
  );
  $("#candidate-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    busy(e.submitter, async () => {
      await api(`/tasks/${t.id}/candidates`, "POST", {
        account_id: f.get("account_id"),
        target: f.get("target"),
        title: f.get("title"),
        text: f.get("text"),
        language: f.get("language"),
        confidence: Number(f.get("confidence")),
        members: Number(f.get("members")),
        activity: Number(f.get("activity")),
        is_public: true,
      });
      if (generation !== panelGeneration || !$("#panel").open) return;
      closePanel();
      await renderTask(t.id);
    });
  };
}
async function renderAPI() {
  const [services, calls] = await Promise.all([
    api("/services"),
    api("/calls"),
  ]);
  $("#main").innerHTML =
    heading(
      "API 中心",
      "五个平台后端随项目安装，统一查看本机能力与调用记录。",
      '<a href="/docs" target="_blank" rel="noopener">OpenAPI 文档 ↗</a>',
    ) +
    `<div class="surface"><div class="section-header"><h2>平台适配器</h2><span class="badge">${services.filter((s) => s.installed).length} / ${services.length} 已集成后端</span></div>${services.map((s) => `<div class="service-row"><div>${platform(s.platform)}<p>${esc(s.transport)}</p></div><div><div class="chips">${s.capabilities.map((c) => `<span class="chip">${actions[c]}</span>`).join("")}</div><p>${esc(s.project)}</p></div><div class="service-status">${s.installed ? '<span class="badge green">后端已安装</span>' : '<span class="badge red">后端缺失</span>'}<p>${s.dependencies_ready === false ? "依赖未安装，请运行 install.ps1" : s.live_configured ? "已启用；账号需通过连接检查" : "依赖就绪；真实执行未启用"}</p></div></div>`).join("")}</div><div class="two-col"><div class="surface padded"><h2>统一接口</h2><div class="log-row"><span><span class="method">GET</span><code>/api/accounts</code></span><span>账号与能力</span></div><div class="log-row"><span><span class="method">POST</span><code>/api/tasks</code></span><span>创建任务</span></div><div class="log-row"><span><span class="method">POST</span><code>/api/tasks/{id}/start</code></span><span>开始发送</span></div><div class="log-row"><span><span class="method">POST</span><code>/api/tasks/{id}/run</code></span><span>执行一轮</span></div><div class="log-row"><span><span class="method">GET</span><code>/api/audit</code></span><span>审计记录</span></div><p class="help mt-16">写入通过明确启动的任务执行。API 凭据由本机配置管理，不在页面展示。</p></div><div class="surface padded"><h2>运行边界</h2><div class="log-row"><span>存储</span><span>${esc(state.overview.storage)}</span></div><div class="log-row"><span>调度</span><span>${state.overview.worker ? "后台 worker" : "手动触发"}</span></div><div class="log-row"><span>未知结果</span><span>只读核对，不自动重发</span></div><div class="log-row"><span>外部发布</span><span>${state.overview.live_writes ? "按账号和任务范围执行" : "关闭；模拟流程可执行"}</span></div></div></div><div class="surface mt-24"><div class="section-header"><h2>最近平台调用</h2><button class="secondary compact" id="refresh-api">刷新</button></div>${
      calls.length
        ? `<div class="table-wrap" role="region" aria-label="数据表格，可横向滚动" tabindex="0"><table><thead><tr><th>时间</th><th>平台</th><th>操作</th><th>结果</th><th>耗时</th><th>错误类型</th><th>执行步骤</th></tr></thead><tbody>${calls
            .slice(0, 40)
            .map(
              (c) =>
                `<tr><td class="nowrap">${fmt(c.at)}</td><td>${names[c.platform]}</td><td><code>${esc(c.operation)}</code></td><td>${badge(c.status === "ok" ? "healthy" : c.status === "error" ? "unavailable" : c.status)}</td><td>${c.duration_ms} ms</td><td><code>${esc(c.error_code || "—")}</code></td><td>${esc(executionSteps[c.execution_step] || c.execution_step || "—")}</td></tr>`,
            )
            .join("")}</tbody></table></div>`
        : empty(
            "暂无平台调用",
            "执行账号检查、发现或任务后，可在这里查看调用记录。",
          )
    }</div>`;
  $("#refresh-api").onclick = () => renderAPI();
}
window.addEventListener("hashchange", () => {
  ++panelGeneration;
  render().catch((e) => toast(e.message, true));
});
(async () => {
  try {
    await api("/session", "POST");
    await refresh();
    await render();
  } catch (e) {
    $("#main").innerHTML = empty(
      "工作区连接失败",
      esc(e.message),
      '<button id="retry-start">重新连接</button>',
    );
    $("#retry-start").onclick = () => location.reload();
  }
})();

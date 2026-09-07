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
};
const actions = {
  discover: "搜索发现",
  join: "入群",
  send_message: "发送文字",
  reply: "回复",
  send_file: "发送文件",
  schedule_message: "原生定时文字",
  schedule_file: "原生定时文件",
  post: "发布文字帖子",
  publish: "发布媒体",
  manual_comment: "人工评论",
  like: "点赞",
  repost: "转发",
  quote: "引用",
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
  discovery: "等待发现审批",
  discovered: "准备外发清单",
  review: "等待外发审批",
  ready: "执行与核验",
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
const api = async (path, method = "GET", body) => {
  const response = await fetch("/api" + path, {
    method,
    headers: { "Content-Type": "application/json", "X-AP-Client": "local-ui" },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const result = await response.json();
  if (!response.ok)
    throw Error(result.error || JSON.stringify(result.detail) || "请求失败");
  return result;
};
function toast(text, error = false) {
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
    : ["unknown", "manual_attention", "missed", "paused", "unchecked"].includes(
          value,
        )
      ? "amber"
      : ["failed_known", "unavailable", "rejected"].includes(value)
        ? "red"
        : ["scheduled", "active", "executing"].includes(value)
          ? "blue"
          : "";
  return `<span class="badge ${color}">${esc(statuses[value] || value)}</span>`;
}
function platform(p) {
  return `<span class="platform"><span class="platform-mark ${esc(p)}">${esc({ telegram: "TG", reddit: "R", instagram: "IG", x: "X" }[p])}</span>${esc(names[p])}</span>`;
}
function heading(title, desc, button = "") {
  return `<div class="page-heading"><div><h1>${title}</h1><p>${desc}</p></div>${button}</div>`;
}
function empty(title, body, button = "") {
  return `<div class="empty"><h2>${title}</h2><p>${body}</p>${button}</div>`;
}
function openPanel(title, html) {
  $("#panel-title").textContent = title;
  $("#panel-content").innerHTML = html;
  $("#panel").showModal();
  setTimeout(() => $("#panel-content input")?.focus(), 10);
}
$("#close-panel").onclick = () => $("#panel").close();
function busy(button, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  return Promise.resolve()
    .then(fn)
    .catch((e) => toast(e.message, true))
    .finally(() => {
      button.disabled = false;
      if (button.textContent === "处理中…") button.textContent = label;
    });
}
async function refresh() {
  [state.accounts, state.overview, state.tasks] = await Promise.all([
    api("/accounts"),
    api("/overview"),
    api("/tasks"),
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
    (state.overview.worker ? "调度器运行中" : "手动运行模式") +
    " · Asia/Shanghai";
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
        : "已恢复已批准任务的执行",
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
    ({ accounts: "账号中心", tasks: "任务中心", api: "API 中心" }[section] ||
      "任务中心");
  if (hash.startsWith("tasks/")) return renderTask(hash.slice(6));
  if (section === "accounts") return renderAccounts();
  if (section === "api") return renderAPI();
  return renderTasks();
}
function renderAccounts() {
  const checked = state.accounts.filter((a) => a.status === "healthy").length;
  $("#main").innerHTML =
    heading(
      "账号中心",
      "连接账号，确认能力，让每一次执行都有明确的身份。",
      '<button id="new-account">添加账号</button>',
    ) +
    `<div class="stats"><span class="stat"><strong>${state.accounts.length}</strong>已登记账号</span><span class="stat"><strong>${checked}</strong>通过身份核验</span><span class="stat"><strong>${state.accounts.filter((a) => a.mode === "live").length}</strong>真实账号</span></div><div class="toolbar"><div class="filter-tabs"><button class="selected" data-mode="all">全部账号</button><button data-mode="live">真实账号</button><button data-mode="simulation">模拟账号</button></div><span class="spacer"></span><button class="secondary compact" id="check-all">检查全部连接</button></div><div class="surface" id="account-list"></div><p class="connect-note">账号中心保留平台标签和健康状态。现有登录会话与凭据由各平台服务管理；模拟账号不会触达外部平台。</p>`;
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
}
function accountRows(mode) {
  const rows = state.accounts.filter((a) => mode === "all" || a.mode === mode);
  $("#account-list").innerHTML = rows.length
    ? `<div class="table-wrap" role="region" aria-label="数据表格，可横向滚动" tabindex="0"><table><thead><tr><th>账号 / 平台</th><th>连接状态</th><th>可接入能力</th><th>执行边界</th><th>操作</th></tr></thead><tbody>${rows.map((a) => `<tr><td>${platform(a.platform)}<span class="sub">${esc(a.label)} · ${a.mode === "simulation" ? "模拟" : "真实"}</span></td><td>${badge(a.status)}${a.disabled ? ' <span class="badge amber">已停用</span>' : ""}<span class="sub">${a.checked_at ? fmt(a.checked_at) : "尚未检查"}${a.cooldown_until > Date.now() / 1000 ? "<br>冷却至 " + fmt(a.cooldown_until) : ""}</span></td><td><div class="chips">${a.capabilities.map((c) => `<span class="chip">${actions[c] || c}</span>`).join("")}</div></td><td>${a.daily_limit} 条 / 日<span class="sub">间隔 ${a.min_interval} 秒</span></td><td><div class="actions"><button class="secondary compact" data-check="${a.id}">检查</button><button class="text-button" data-disable="${a.id}" data-value="${!a.disabled}">${a.disabled ? "启用" : "停用"}</button></div></td></tr>`).join("")}</tbody></table></div>`
    : empty("暂无账号", "添加现有平台账号，或创建模拟账号测试完整流程。");
  document.querySelectorAll("[data-check]").forEach(
    (b) =>
      (b.onclick = () =>
        busy(b, async () => {
          const r = await api(`/accounts/${b.dataset.check}/check`, "POST");
          await refresh();
          accountRows(mode);
          toast(
            r.status === "healthy"
              ? "账号身份与连接已核验"
              : "服务或账号尚未通过核验：" + (r.health.reason || "unknown"),
            r.status !== "healthy",
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
function accountForm() {
  openPanel(
    "添加平台账号",
    `<form id="account-form"><div class="field-grid"><div><label for="platform">平台</label><select id="platform" name="platform">${Object.entries(
      names,
    )
      .map(([k, v]) => `<option value="${k}">${v}</option>`)
      .join(
        "",
      )}</select></div><div><label for="mode">连接方式</label><select name="mode" id="mode"><option value="live">现有真实账号</option><option value="simulation">模拟账号 · 无外部操作</option></select></div><div><label for="label">显示名称</label><input name="label" id="label" required placeholder="例如：品牌运营账号"></div><div><label for="backend-label">平台账号标签</label><input name="backend_label" id="backend-label" required placeholder="现有服务的账号标签或用户名"><span class="help">Telegram 使用服务标签；Instagram 使用用户名。</span></div><div><label for="daily">每日限额</label><input name="daily_limit" id="daily" type="number" value="5" min="1" max="100" required></div><div><label for="interval">最小间隔（秒）</label><input name="min_interval" id="interval" type="number" value="120" min="0" required></div></div><div class="form-error" id="account-error" role="alert"></div><div class="form-actions"><button type="submit">保存账号</button></div></form>`,
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
      $("#panel").close();
      await refresh();
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
      "从发现到发布，把审批、排期与每一项结果放在一起。",
      '<button id="new-task">创建任务</button>',
    ) +
    `<div class="stats"><span class="stat"><strong>${state.tasks.length}</strong>全部任务</span><span class="stat"><strong>${active}</strong>进行中</span><span class="stat"><strong>${state.tasks.filter((t) => t.phase === "review").length}</strong>等待外发审批</span></div>${state.overview.global_stop ? '<div class="banner warning">任务执行当前暂停。可以继续创建、编辑和审批；恢复后才会执行已批准的项目。</div>' : ""}<div class="toolbar"><div class="filter-tabs"><button class="selected" id="current-tasks">当前项目</button><button id="legacy-tasks">历史任务 · 只读</button></div><span class="spacer"></span><input id="task-search" aria-label="搜索任务" placeholder="搜索任务名称或 ID"></div><div class="surface" id="task-list"></div>`;
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
            }</span></td><td>${badge(t.state)}</td><td class="nowrap">${fmt(t.created_at)}</td></tr>`,
        )
        .join("")}</tbody></table></div>`
    : empty(
        "开始第一个推广任务",
        "选择账号，定义发现范围，再准备和批准执行清单。",
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
  openPanel(
    "创建推广任务",
    `<form id="task-form"><div class="field-grid"><div class="full"><label for="task-name">任务名称</label><input id="task-name" name="name" required placeholder="例如：AI 视频创作者互动 · 第一批"></div><fieldset class="full"><legend>选择执行账号</legend>${state.accounts.map((a) => `<label class="checkbox-label"><input type="checkbox" name="accounts" value="${a.id}">${esc(a.label)} · ${names[a.platform]} · ${a.mode === "simulation" ? "模拟" : "真实"}</label>`).join("")}</fieldset><div class="full"><label for="queries">发现主题（每行一个）</label><textarea id="queries" name="queries" placeholder="AI video creation" rows="2"></textarea></div><div><label for="required">必需关键词（逗号分隔）</label><input id="required" name="required" placeholder="AI, video"></div><div><label for="blocked">排除关键词（逗号分隔）</label><input id="blocked" name="blocked" placeholder="airdrop, giveaway"></div><div><label for="starts">执行开始时间</label><input id="starts" name="starts" type="datetime-local" value="${isoLocal(now)}" required></div><div><label for="ends">执行结束时间</label><input id="ends" name="ends" type="datetime-local" value="${isoLocal(now + 86400000)}" required></div><div><label for="languages">允许语言</label><input id="languages" name="languages" value="en, zh" required></div><div><label for="candidates">最多候选数</label><input id="candidates" name="candidates" type="number" min="1" max="200" value="20" required></div><div><label for="task-daily">每日外发限额</label><input id="task-daily" name="daily" type="number" value="5" min="1" max="100" required></div><div><label for="task-interval">最小间隔（秒）</label><input id="task-interval" name="interval" type="number" value="120" min="0" required></div></div><details><summary>更多发现边界</summary><div class="field-grid"><div><label>最低成员数<input name="min_members" type="number" value="0" min="0"></label></div><div><label>最低活跃度<input name="min_activity" type="number" value="0" min="0"></label></div><div><label>最低置信度<input name="confidence" type="number" value="0.8" min="0.5" max="1" step="0.05"></label></div><div><label>最多 Telegram 入群数<input name="max_joins" type="number" value="0" min="0" max="20"></label></div><div class="full"><label>目标白名单（每行一个，空白表示使用发现政策）<textarea name="targets" rows="2"></textarea></label></div></div></details><p class="help">仅公开目标。输入时间使用浏览器本地时区，保存时转换为明确的 UTC 时间；任务详情统一显示北京时间。</p><div class="form-actions"><button type="submit">创建并预览发现政策</button></div></form>`,
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
          daily_limit: Number(f.get("daily")),
          target_allowlist: split("targets"),
        },
      });
      $("#panel").close();
      await refresh();
      location.hash = "tasks/" + task.id;
    });
  };
}
async function renderTask(id) {
  const t = await api("/tasks/" + id);
  state.current = t;
  const discovery = t.approvals.some((a) => a.stage === "discovery_join");
  const approved = t.approvals.some((a) => a.stage === "outbound");
  const ended = ["cancelled", "completed"].includes(t.state);
  $("#main").innerHTML =
    `<div class="detail-top"><a href="#tasks">任务中心</a><span class="muted">/</span><code>${esc(t.id)}</code>${t.mode === "simulation" ? '<span class="badge amber">模拟任务 · 无外部发布</span>' : ""}</div>` +
    heading(
      esc(t.name),
      "计划时间以北京时间显示；逐项保存执行结果和核验记录。",
      `<div class="actions">${badge(t.state)}<button class="secondary compact" id="refresh-task">刷新</button></div>`,
    ) +
    `<div class="steps"><span class="${discovery ? "done" : ""}"><b>1</b>发现政策</span><span class="${t.manifest ? "done" : ""}"><b>2</b>准备清单</span><span class="${approved ? "done" : ""}"><b>3</b>外发审批</span><span class="${t.state === "completed" ? "done" : ""}"><b>4</b>执行核验</span></div><div class="detail-grid"><div class="detail-body"><div class="surface"><div class="section-header"><h2>执行清单 <span class="muted">${t.items.length}</span></h2>${discovery && !t.manifest && !ended ? '<button id="edit-items" class="secondary compact">编辑执行项</button>' : ""}</div>${t.items.length ? t.items.map((item) => itemPreview(item)).join("") : empty("尚无执行项", discovery ? "添加每项的目标、内容、媒体和时间，再冻结外发清单。" : "先检查任务审批中的发现政策并完成第一阶段审批。")}</div>${discovery && !t.manifest && t.items.length ? '<div class="form-actions"><button id="prepare">冻结并预览外发清单</button></div>' : ""}<div class="surface mt-24"><div class="section-header"><h2>发现候选 <span class="muted">${t.candidates.length}</span></h2></div><div class="padded">${
      discovery && !ended
        ? `<div class="actions">${t.account_snapshot
            .filter((a) => a.platform !== "instagram")
            .map(
              (a) =>
                `<button class="secondary compact" data-discover="${a.id}">搜索 ${names[a.platform]}</button>`,
            )
            .join(
              "",
            )}<button class="secondary compact" id="add-candidate">录入候选</button>${t.policy.max_joins ? '<button class="secondary compact" id="join-candidates">执行已批准入群</button>' : ""}</div>`
        : ""
    }${t.candidates.length ? '<div class="table-wrap" role="region" aria-label="数据表格，可横向滚动" tabindex="0"><table><thead><tr><th>候选目标</th><th>资格判断</th><th>原因</th></tr></thead><tbody>' + t.candidates.map((c) => `<tr><td>${esc(c.target)}</td><td>${badge(c.decision)}</td><td>${esc(c.reasons.join(" · ") || "通过发现政策")}<span class="sub">${esc(c.state)}</span>${["unknown", "pending_admin_approval"].includes(c.state) ? `<button class="text-button" data-join-reconcile="${c.id}">只读核对入群</button>` : ""}</td></tr>`).join("") + "</tbody></table></div>" : '<p class="muted mt-14">搜索结果先经过资格筛选，低置信度候选会被记录为拒绝。</p>'}</div></div><div class="surface mt-24"><div class="section-header"><h2>任务日志</h2></div><div class="padded" id="task-audit">正在读取…</div></div></div><aside class="detail-aside"><div class="surface padded"><h2>任务审批</h2><div class="approval"><strong>发现与入群政策</strong>${discovery ? " " + badge("confirmed").replace("已发布 · 已核验", "已批准") : ""}<details ${!discovery ? "open" : ""}><summary>查看完整范围</summary><p class="help">账号：${t.account_snapshot.map((a) => esc(a.label) + " (" + names[a.platform] + ")").join("、")}</p><pre>${esc(JSON.stringify(t.policy, null, 2))}</pre></details>${!discovery && !ended ? approvalForm(t, "discovery_join", t.discovery_phrase) : ""}</div><div class="approval"><strong>最终外发批次</strong>${approved ? ' <span class="badge green">已批准</span>' : ""}${t.manifest ? `<details><summary>查看完整不可变清单</summary><pre>${esc(JSON.stringify(t.manifest, null, 2))}</pre></details><span class="help">SHA-256</span><code>${esc(t.manifest_hash)}</code>` : '<p class="help">保存执行项并冻结后，可审核第二阶段。</p>'}${t.manifest && !approved && !ended ? approvalForm(t, "outbound", t.outbound_phrase) : ""}</div></div><div class="surface padded mt-20"><h2>执行控制</h2><p class="help">${state.overview.global_stop ? "全局执行已暂停。请使用顶部开关恢复。" : "调度器按批准的时间窗口执行。"}</p><div class="actions mt-14">${approved && !ended ? '<button id="run-task" class="compact">运行一轮</button>' : ""}${!ended ? `<button class="secondary compact" id="pause-task">${t.state === "paused" ? "恢复任务" : "暂停任务"}</button><button class="danger compact" id="cancel-task">取消任务</button>` : ""}</div><p class="native-note">停止本地任务不会撤回已经提交给 Telegram 的原生排期。</p></div></aside></div>`;
  $("#refresh-task").onclick = () => renderTask(id);
  $("#edit-items")?.addEventListener("click", () => itemEditor(t));
  $("#prepare")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      await api(`/tasks/${id}/prepare`, "POST");
      await renderTask(id);
    }),
  );
  document.querySelectorAll("[data-approval]").forEach(
    (form) =>
      (form.onsubmit = (e) => {
        e.preventDefault();
        busy(e.submitter, async () => {
          await api(`/tasks/${id}/approve/${form.dataset.approval}`, "POST", {
            phrase: new FormData(form).get("phrase"),
          });
          await refresh();
          await renderTask(id);
          toast("精确授权已持久保存");
        });
      }),
  );
  $("#run-task")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      const r = await api(`/tasks/${id}/run`, "POST");
      await refresh();
      await renderTask(id);
      toast(
        r.processed.length
          ? `本轮处理 ${r.processed.length} 项`
          : "暂无可执行项：请检查时间、审批、全局开关及账号状态",
      );
    }),
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
          await renderTask(id);
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
          await renderTask(id);
          toast("候选已筛选并记录");
        })),
  );
  $("#join-candidates")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      await api(`/tasks/${id}/join`, "POST");
      await renderTask(id);
      toast("合格候选入群批次已处理");
    }),
  );
  document.querySelectorAll("[data-join-reconcile]").forEach(
    (b) =>
      (b.onclick = () =>
        busy(b, async () => {
          await api(`/candidates/${b.dataset.joinReconcile}/reconcile`, "POST");
          await renderTask(id);
          toast("已只读核对成员状态；不会重新入群");
        })),
  );
  $("#add-candidate")?.addEventListener("click", () => candidateForm(t));
  const logs = await api("/audit?task_id=" + encodeURIComponent(id));
  if (state.current?.id === id && $("#task-audit"))
    $("#task-audit").innerHTML = logs.length
      ? logs
          .slice(0, 25)
          .map(
            (r) =>
              `<div class="log-row"><span>${esc(r.kind)}<br><code>${esc(JSON.stringify(r.details))}</code></span><span class="muted nowrap">${fmt(r.at)}</span></div>`,
          )
          .join("")
      : "暂无记录";
}
function approvalForm(t, stage, phrase) {
  return `<form data-approval="${stage}"><code class="phrase">${esc(phrase)}</code><label for="phrase-${stage}">核对完整范围后，输入确认短语</label><input id="phrase-${stage}" name="phrase" required autocomplete="off" placeholder="粘贴完整短语"><button class="compact mt-10" type="submit">批准${stage === "outbound" ? "外发批次" : "发现政策"}</button></form>`;
}
function itemPreview(i) {
  const p = i.payload;
  return `<article class="item-preview"><div class="item-head"><strong>${i.position + 1}. ${esc(p.account_label)} · ${actions[p.action] || esc(p.action)}</strong>${badge(i.status)}</div><div class="item-meta"><span>目标：${esc(p.target)}</span><span>${fmt(p.scheduled_at)} UTC+08:00</span></div>${p.title ? `<h3>${esc(p.title)}</h3>` : ""}${p.text ? `<div class="item-text">${esc(p.text)}</div>` : ""}${p.media.length ? '<p class="help">媒体：' + p.media.map((m) => esc(m.filename) + " · " + esc(m.sha256.slice(0, 12))).join("，") + "</p>" : ""}${i.external_reference ? '<p class="help">回执：<code>' + esc(i.external_reference) + "</code></p>" : ""}${i.result.reason ? '<p class="help">' + esc(i.result.reason) + "</p>" : ""}<div class="actions">${["unknown", "scheduled"].includes(i.status) ? `<button class="text-button" data-reconcile="${i.id}">只读核对结果</button>` : ""}${i.status === "manual_attention" ? `<button class="text-button" data-manual="${i.id}">登记人工发布结果</button>` : ""}</div></article>`;
}
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
    return {
      account_id: a.id,
      action: state.accounts
        .find((x) => x.id === a.id)
        .capabilities.find((c) => !["discover", "join"].includes(c)),
      target: "",
      text: "",
      title: "",
      media: [],
      scheduled_at: new Date(
        Math.max(Date.now() + 60000, new Date(t.policy.starts_at).getTime()),
      ).toISOString(),
      reply_to: null,
      recipient_id: "",
      target_owned: false,
    };
  }
  if (!items.length) items.push(blank());
  openPanel("编辑执行清单", "");
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
            )}</select></label></div><div class="full"><label>目标<input name="target" value="${esc(p.target)}" placeholder="群组 @username、帖子链接、社区名称或自有账号 ID" required></label></div><div class="full"><label>标题（帖子需要）<input name="title" value="${esc(p.title)}"></label></div><div class="full"><label>完整正文 / Caption<textarea name="text">${esc(p.text)}</textarea></label></div><div><label>计划时间<input name="scheduled_at" type="datetime-local" value="${isoLocal(new Date(p.scheduled_at).getTime())}" required></label></div><div><label>Telegram 原消息 ID（回复时）<input name="reply_to" type="number" min="1" value="${p.reply_to || ""}"></label></div><div class="full"><label class="checkbox-label"><input type="checkbox" name="target_owned" ${p.target_owned ? "checked" : ""}>这是自有发布目标（Instagram 发布必须勾选）</label></div><div class="full"><label>媒体文件<input type="file" data-upload="${n}"></label><p class="help">${p.media.map((m) => esc(m.filename)).join(" · ") || "尚未添加媒体"} · 最大 50 MB</p>${p.media.map((m, j) => `<label>媒体 ${j + 1} 的 HTTPS 地址（Instagram 必需）<input data-source="${j}" value="${esc(m.source_url)}" placeholder="https://..."></label><label>媒体类型<select data-media-type="${j}"><option value="">按文件类型</option>${["IMAGE", "VIDEO", "REELS"].map((v) => `<option ${m.media_type === v ? "selected" : ""}>${v}</option>`).join("")}</select></label>`).join("")}</div></div></fieldset>`,
      )
      .join(
        "",
      )}<button class="secondary" type="button" id="append-item">添加执行项</button><div class="form-actions"><button type="submit">保存清单</button></div></form>`;
    function collect() {
      container.querySelectorAll("[data-index]").forEach((row) => {
        const n = Number(row.dataset.index),
          p = items[n];
        for (const name of ["account_id", "action", "target", "title", "text"])
          p[name] = row.querySelector(`[name=${name}]`).value;
        p.scheduled_at = new Date(
          row.querySelector("[name=scheduled_at]").value,
        ).toISOString();
        p.reply_to = Number(row.querySelector("[name=reply_to]").value) || null;
        p.target_owned = row.querySelector("[name=target_owned]").checked;
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
          draw();
        }),
    );
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
            if (!response.ok) throw Error(m.error);
            items[Number(el.dataset.upload)].media.push(m);
            draw();
          } catch (e) {
            toast(e.message, true);
          }
        }),
    );
    $("#items-form").onsubmit = (e) => {
      e.preventDefault();
      busy(e.submitter, async () => {
        collect();
        await api(`/tasks/${t.id}/items`, "PUT", items);
        $("#panel").close();
        await renderTask(t.id);
      });
    };
  }
  draw();
}
function manualForm(item) {
  openPanel(
    "登记人工发布结果",
    `<p class="help">先核对批准文本，再在平台手动发布。登记后不会自动重试。</p><div class="item-text">${esc(item.payload.text)}</div><form id="manual-form"><label>平台上实际发布的完整文本<textarea name="text" required></textarea></label><label>Instagram 公开链接<input name="url" type="url" required></label><label class="checkbox-label"><input name="verified" type="checkbox">已在平台页面核验发布结果</label><div class="form-actions"><button>保存结果</button></div></form>`,
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
      $("#panel").close();
      await renderTask(item.task_id);
    });
  };
}
function candidateForm(t) {
  openPanel(
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
      $("#panel").close();
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
      "统一平台能力与调用记录，连接现有服务和新的任务流程。",
      '<a href="/docs" target="_blank" rel="noopener">OpenAPI 文档 ↗</a>',
    ) +
    `<div class="surface"><div class="section-header"><h2>平台适配器</h2><span class="badge">${services.filter((s) => s.installed).length} / 4 已发现源码</span></div>${services.map((s) => `<div class="service-row"><div>${platform(s.platform)}<p>${esc(s.transport)}</p></div><div><div class="chips">${s.capabilities.map((c) => `<span class="chip">${actions[c]}</span>`).join("")}</div><p>${esc(s.project)}</p></div><div class="service-status">${s.installed ? '<span class="badge green">已接入实现</span>' : '<span class="badge red">项目缺失</span>'}<p>${s.live_configured ? "真实执行配置就绪" : "真实执行尚未接管"}</p></div></div>`).join("")}</div><div class="two-col"><div class="surface padded"><h2>统一接口</h2><div class="log-row"><span><span class="method">GET</span><code>/api/accounts</code></span><span>账号与能力</span></div><div class="log-row"><span><span class="method">POST</span><code>/api/tasks</code></span><span>创建任务</span></div><div class="log-row"><span><span class="method">POST</span><code>/api/tasks/{id}/approve/{stage}</code></span><span>精确审批</span></div><div class="log-row"><span><span class="method">POST</span><code>/api/tasks/{id}/run</code></span><span>执行一轮</span></div><div class="log-row"><span><span class="method">GET</span><code>/api/audit</code></span><span>审计记录</span></div><p class="help mt-16">写入只能通过批准的任务路径。API 凭据由本机配置管理，不在页面展示。</p></div><div class="surface padded"><h2>运行边界</h2><div class="log-row"><span>存储</span><span>${esc(state.overview.storage)}</span></div><div class="log-row"><span>调度</span><span>${state.overview.worker ? "后台 worker" : "手动触发"}</span></div><div class="log-row"><span>未知结果</span><span>只读核对，不自动重发</span></div><div class="log-row"><span>外部发布</span><span>${state.overview.live_writes ? "按账号和任务权限执行" : "关闭；模拟流程可执行"}</span></div></div></div><div class="surface mt-24"><div class="section-header"><h2>最近平台调用</h2><button class="secondary compact" id="refresh-api">刷新</button></div>${
      calls.length
        ? `<div class="table-wrap" role="region" aria-label="数据表格，可横向滚动" tabindex="0"><table><thead><tr><th>时间</th><th>平台</th><th>操作</th><th>结果</th><th>耗时</th><th>错误类型</th></tr></thead><tbody>${calls
            .slice(0, 40)
            .map(
              (c) =>
                `<tr><td class="nowrap">${fmt(c.at)}</td><td>${names[c.platform]}</td><td><code>${esc(c.operation)}</code></td><td>${badge(c.status === "ok" ? "healthy" : "unavailable")}</td><td>${c.duration_ms} ms</td><td><code>${esc(c.error_code || "—")}</code></td></tr>`,
            )
            .join("")}</tbody></table></div>`
        : empty(
            "暂无平台调用",
            "执行账号检查、发现或任务后，可在这里查看调用记录。",
          )
    }</div>`;
  $("#refresh-api").onclick = () => renderAPI();
}
window.addEventListener("hashchange", () =>
  render().catch((e) => toast(e.message, true)),
);
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

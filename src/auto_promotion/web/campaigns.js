"use strict";
const poolState = { pools: [], selected: new Set(), contentOffset: 0 };
const lines = (value) =>
  String(value || "")
    .split(/\r?\n|,/)
    .map((s) => s.trim())
    .filter(Boolean);
const listField = (label, name, value = []) =>
  `<div class="full"><label for="field-${name}">${label}</label><textarea id="field-${name}" name="${name}" rows="2">${esc(value.join("\n"))}</textarea></div>`;

async function renderPools() {
  await refresh();
  poolState.pools = await api("/pools");
  $("#main").innerHTML =
    heading(
      "号池与人设",
      "按业务组织账号，为内容分配配置真实角色、专业范围和表达方式。",
      '<button id="new-pool">创建号池</button>',
    ) +
    `<div class="stats"><span class="stat"><strong>${poolState.pools.length}</strong>号池</span><span class="stat"><strong>${state.accounts.length}</strong>已登记账号</span></div>` +
    (poolState.pools.length
      ? poolState.pools
          .map(
            (p) =>
              `<section class="surface padded mt-20"><div class="section-header"><h2>${esc(p.name)}</h2><button class="secondary compact" data-pool-edit="${p.id}">编辑号池</button></div><p>${esc(p.description)}</p><p class="help">${p.accounts.length} 个账号 · ${p.limits.daily} 次 / 日 · ${p.limits.hourly} 次 / 小时</p><div class="table-wrap"><table><thead><tr><th>账号</th><th>平台与模式</th><th>状态</th><th>人设</th></tr></thead><tbody>${p.accounts.map((a) => `<tr><td>${esc(a.label)}</td><td>${platform(a.platform)} · ${a.mode === "simulation" ? "模拟" : "真实"}</td><td>${badge(a.status)}</td><td><button class="secondary compact" data-profile="${a.id}">配置人设</button></td></tr>`).join("")}</tbody></table></div></section>`,
          )
          .join("")
      : empty(
          "尚无号池",
          "先在账号中心登记账号，再创建号池并配置每个账号的人设。",
        ));
  $("#new-pool").onclick = () => poolForm();
  document
    .querySelectorAll("[data-pool-edit]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          poolForm(poolState.pools.find((p) => p.id === b.dataset.poolEdit))),
    );
  document
    .querySelectorAll("[data-profile]")
    .forEach(
      (b) => (b.onclick = () => busy(b, () => profileForm(b.dataset.profile))),
    );
}

function poolForm(pool) {
  openPanel(
    pool ? "编辑号池" : "创建号池",
    `<form id="pool-form"><div class="field-grid"><div class="full"><label>号池名称<input name="name" required maxlength="160" value="${esc(pool?.name || "")}"></label></div><div class="full"><label>业务说明<textarea name="description">${esc(pool?.description || "")}</textarea></label></div><div><label>号池每日额度<input name="daily" type="number" min="1" max="100000" value="${pool?.limits.daily || 1000}" required></label></div><div><label>号池每小时额度<input name="hourly" type="number" min="1" max="10000" value="${pool?.limits.hourly || 100}" required></label></div><fieldset class="full"><legend>选择账号</legend>${state.accounts.map((a) => `<label class="checkbox-label"><input type="checkbox" name="accounts" value="${a.id}" ${pool?.account_ids.includes(a.id) ? "checked" : ""}>${esc(a.label)} · ${esc(names[a.platform])} · ${a.mode === "simulation" ? "模拟" : "真实"}</label>`).join("")}</fieldset></div><p class="help">一个活动使用同一种模式的账号。修改号池后，未生成执行批次的活动需要重新规划。</p><div class="form-actions"><button>保存号池</button></div></form>`,
  );
  $("#pool-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    busy(e.submitter, async () => {
      await api(pool ? `/pools/${pool.id}` : "/pools", pool ? "PUT" : "POST", {
        name: f.get("name"),
        description: f.get("description"),
        account_ids: f.getAll("accounts"),
        limit: {
          daily: Number(f.get("daily")),
          hourly: Number(f.get("hourly")),
        },
      });
      $("#panel").close();
      await renderPools();
    });
  };
}

async function profileForm(accountId) {
  const record = await api(`/accounts/${accountId}/profile`),
    p = record.payload;
  const account = state.accounts.find((a) => a.id === accountId);
  openPanel(
    "账号人设 · " + account.label,
    `<form id="profile-form"><p class="help">当前版本 ${record.version}。已生成执行批次保留原人设快照，新版本用于后续草稿。</p><div class="field-grid"><div><label>品牌<input name="brand" value="${esc(p.brand)}"></label></div><div><label>真实业务角色<input name="role" value="${esc(p.role)}" placeholder="技术支持 / 官方产品 / 品牌创作者"></label></div>${listField("语言代码（每行一个）", "languages", p.languages)}${listField("专业领域与主题", "topics", p.topics)}${listField("受众", "audience", p.audience)}<div class="full"><label for="profile-tone">表达语气</label><textarea id="profile-tone" name="tone">${esc(p.tone)}</textarea></div><div class="full"><label for="profile-facts">可引用的产品事实</label><textarea id="profile-facts" name="facts" rows="3">${esc(p.facts)}</textarea></div><div class="full"><label for="profile-reply_template">回复草稿模板</label><textarea id="profile-reply_template" name="reply_template" rows="4">${esc(p.reply_template)}</textarea><span class="help">可使用 {title}、{context}、{topic}、{brand}、{role}、{facts}、{author}。已导入的精确正文优先；模板草稿需在批次中审核。</span></div>${listField("禁止关键词", "blocked_keywords", p.blocked_keywords)}${listField("允许目标（留空表示按活动政策筛选）", "allowed_targets", p.allowed_targets)}<div><label>工作开始小时<input name="work_start_hour" type="number" min="0" max="23" required value="${p.work_start_hour}"></label></div><div><label>工作结束小时<input name="work_end_hour" type="number" min="1" max="24" required value="${p.work_end_hour}"></label></div><div class="full"><label>工作时区<input name="timezone" required value="${esc(p.timezone)}"></label></div></div><div class="form-actions"><button>保存人设</button></div></form>`,
  );
  $("#profile-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target),
      value = {};
    for (const key of [
      "brand",
      "role",
      "tone",
      "facts",
      "reply_template",
      "timezone",
    ])
      value[key] = f.get(key);
    for (const key of [
      "languages",
      "topics",
      "audience",
      "blocked_keywords",
      "allowed_targets",
    ])
      value[key] = lines(f.get(key));
    for (const key of ["work_start_hour", "work_end_hour"])
      value[key] = Number(f.get(key));
    busy(e.submitter, async () => {
      await api(`/accounts/${accountId}/profile`, "PUT", value);
      $("#panel").close();
      toast("人设已保存，新草稿使用新版本");
    });
  };
}

async function renderContent(offset = poolState.contentOffset) {
  poolState.contentOffset = offset;
  const data = await api(`/opportunities?offset=${offset}&limit=100`);
  $("#main").innerHTML =
    heading(
      "共享内容池",
      "统一保存待互动内容、作者、有效期和许可证据，跨号池与活动去重。",
      '<div class="actions"><button class="secondary" id="collect-content">读取 Telegram 群消息</button><button class="secondary" id="import-content">导入 JSON / CSV</button><button id="content-campaign">创建活动</button></div>',
    ) +
    `<div class="toolbar"><strong>${data.total} 条内容 · 已选择 <span id="selected-count">${poolState.selected.size}</span> 条</strong><span class="spacer"></span><button class="secondary compact" id="select-page">选择本页</button><button class="secondary compact" id="select-all-content">选择全部内容</button><button class="text-button" id="clear-content">清空选择</button></div>` +
    (data.items.length
      ? `<div class="surface table-wrap"><table><thead><tr><th>选择</th><th>内容</th><th>平台 / 作者</th><th>资格证据</th><th>有效期</th></tr></thead><tbody>${data.items.map((o) => `<tr><td><input type="checkbox" aria-label="选择 ${esc(o.payload.title || o.payload.external_id)}" data-content="${o.id}" ${poolState.selected.has(o.id) ? "checked" : ""}></td><td><strong>${esc(o.payload.title || o.payload.external_id)}</strong><span class="sub content-wrap">${esc(o.payload.target)}</span><span class="sub">${esc(o.payload.topics.join(" · "))}</span></td><td>${platform(o.platform)}<span class="sub">${esc(o.payload.author_id || "作者未登记")}</span></td><td>${esc(o.payload.language)} · ${o.payload.confidence}<span class="sub">${esc({ allowed: "许可已登记", denied: "不允许互动", unknown: "许可待核实" }[o.payload.permission])}</span></td><td>${fmt(o.payload.expires_at)}</td></tr>`).join("")}</tbody></table></div>`
      : empty(
          "尚无内容",
          "可一次导入最多5000条 JSON 或 CSV 数据；相同平台内容ID会合并。",
        )) +
    `<div class="form-actions"><button class="secondary" id="content-prev" ${offset === 0 ? "disabled" : ""}>上一页</button><span>${offset + 1}–${Math.min(offset + 100, data.total)} / ${data.total}</span><button class="secondary" id="content-next" ${offset + 100 >= data.total ? "disabled" : ""}>下一页</button></div>`;
  const updateCount = () =>
    ($("#selected-count").textContent = poolState.selected.size);
  document.querySelectorAll("[data-content]").forEach(
    (b) =>
      (b.onchange = () => {
        b.checked
          ? poolState.selected.add(b.dataset.content)
          : poolState.selected.delete(b.dataset.content);
        updateCount();
      }),
  );
  $("#select-page").onclick = () => {
    data.items.forEach((o) => poolState.selected.add(o.id));
    renderContent(offset).catch((e) => toast(e.message, true));
  };
  $("#select-all-content").onclick = (e) =>
    busy(e.target, async () => {
      for (let n = 0; n < data.total; n += 500) {
        const page = await api(`/opportunities?offset=${n}&limit=500`);
        page.items.forEach((o) => poolState.selected.add(o.id));
      }
      await renderContent(offset);
    });
  $("#clear-content").onclick = () => {
    poolState.selected.clear();
    renderContent(offset).catch((e) => toast(e.message, true));
  };
  $("#content-prev").onclick = () => renderContent(Math.max(0, offset - 100));
  $("#content-next").onclick = () => renderContent(offset + 100);
  $("#import-content").onclick = contentImportForm;
  $("#collect-content").onclick = (e) => busy(e.target, collectContentForm);
  $("#collect-content").insertAdjacentHTML(
    "beforebegin",
    '<a href="#feeds">定时采集来源</a>',
  );
  document.querySelectorAll("[data-content]").forEach((checkbox) => {
    const opportunity = data.items.find(
      (o) => o.id === checkbox.dataset.content,
    );
    const button = document.createElement("button");
    button.className = "text-button";
    button.textContent = "核对资格与文案";
    button.onclick = () => contentReviewForm(opportunity);
    checkbox.closest("tr").cells[3].append(button);
  });
  $("#content-campaign").onclick = (e) => busy(e.target, campaignForm);
}

function csvItems(text) {
  const rows = [];
  let row = [],
    cell = "",
    quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') {
      if (quoted && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else quoted = !quoted;
    } else if (c === "," && !quoted) {
      row.push(cell);
      cell = "";
    } else if ((c === "\n" || c === "\r") && !quoted) {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      if (row.some(Boolean)) rows.push(row);
      row = [];
      cell = "";
    } else cell += c;
  }
  if (quoted) throw Error("CSV 引号未闭合");
  row.push(cell);
  if (row.some(Boolean)) rows.push(row);
  const headers =
    rows.shift()?.map((s) => s.replace(/^\uFEFF/, "").trim()) || [];
  return rows.map((cells) => {
    const item = {};
    headers.forEach((key, n) => {
      let value = cells[n];
      if (value === undefined || value === "") return;
      if (["topics", "audience", "permission_account_ids"].includes(key))
        value = value.startsWith("[")
          ? JSON.parse(value)
          : value
              .split("|")
              .map((s) => s.trim())
              .filter(Boolean);
      if (
        ["confidence", "value", "members", "activity", "reply_to"].includes(key)
      )
        value = Number(value);
      if (["is_public", "target_owned"].includes(key)) {
        if (!["true", "false", "1", "0"].includes(value.toLowerCase()))
          throw Error("CSV 布尔值需要 true / false");
        value = ["true", "1"].includes(value.toLowerCase());
      }
      if (key === "media") value = JSON.parse(value);
      item[key] = value;
    });
    return item;
  });
}

function contentImportForm() {
  const example = [
    {
      platform: "telegram",
      external_id: "sample-message-123",
      target: "@example_group",
      action: "reply",
      reply_to: 123,
      author_id: "example-author",
      title: "AI video question",
      context: "How do I export an AI video?",
      language: "en",
      topics: ["AI video"],
      confidence: 0.9,
      permission: "unknown",
      draft_text: "",
    },
  ];
  openPanel(
    "导入共享内容",
    `<form id="content-import-form"><label>读取本机 JSON 或 CSV 文件<input id="content-file" type="file" accept=".json,.csv,application/json,text/csv"></label><label for="content-json">JSON 数组或 {items: [...]}</label><textarea id="content-json" rows="12" spellcheck="false" required>${esc(JSON.stringify(example, null, 2))}</textarea><p class="help">必须填写 platform、external_id、target。Telegram 回复填写 reply_to，Discord 回复填写 discord_reply_to。真实自动互动还需 permission=allowed、permission_evidence，并可用 permission_account_ids 限定账号。CSV 数组字段使用 | 分隔。正文留空时使用账号模板。</p><div class="form-actions"><button>导入并去重</button></div></form>`,
  );
  $("#content-file").onchange = async (e) => {
    try {
      const file = e.target.files[0];
      if (!file) return;
      const text = await file.text();
      $("#content-json").value = file.name.toLowerCase().endsWith(".csv")
        ? JSON.stringify(csvItems(text), null, 2)
        : text;
    } catch (error) {
      toast(error.message, true);
    }
  };
  $("#content-import-form").onsubmit = (e) => {
    e.preventDefault();
    busy(e.submitter, async () => {
      const value = JSON.parse($("#content-json").value);
      const result = await api("/opportunities/import", "POST", {
        items: Array.isArray(value) ? value : value.items,
      });
      result.ids.forEach((id) => poolState.selected.add(id));
      $("#panel").close();
      poolState.contentOffset = 0;
      location.hash = "content";
      await renderContent(0);
      toast(
        `新增 ${result.imported} · 更新 ${result.updated} · 重复 ${result.duplicates}`,
      );
    });
  };
}

async function renderCampaigns() {
  const campaigns = await api("/campaigns");
  $("#main").innerHTML =
    heading(
      "推广活动",
      "从共享内容池选择内容，按账号人设分配，再创建精确执行批次。",
      '<div class="actions"><button class="secondary" id="execution-settings">调度与额度</button><button id="new-campaign">创建活动</button></div>',
    ) +
    (campaigns.length
      ? `<div class="surface table-wrap"><table><thead><tr><th>活动</th><th>状态</th><th>已分配 / 未分配</th><th>执行批次</th><th>执行结果</th></tr></thead><tbody>${campaigns.map((c) => `<tr><td><a href="#campaigns/${c.id}">${esc(c.name)}</a><span class="sub">${fmt(c.created_at)}</span></td><td>${esc({ draft: "待规划", planned: "待查看分配", materialized: "发送与执行", paused: "已暂停", cancelled: "已取消", completed: "已完成" }[c.state] || c.state)}</td><td>${(c.summary.planned || 0) + (c.summary.batched || 0)} / ${c.summary.unassigned || 0}</td><td>${c.tasks.length}</td><td>确认 ${c.execution_summary.confirmed || 0}<span class="sub">未知 ${c.execution_summary.unknown || 0} · 人工 ${c.execution_summary.manual_attention || 0}</span></td></tr>`).join("")}</tbody></table></div>`
      : empty(
          "尚无活动",
          "先建立号池和人设，再从共享内容池选择待互动内容。",
          '<a href="#content">前往共享内容池</a>',
        ));
  $("#new-campaign").onclick = (e) => busy(e.target, campaignForm);
  $("#execution-settings").onclick = (e) =>
    busy(e.target, executionSettingsForm);
}

async function campaignForm() {
  if (!poolState.selected.size) {
    toast("请先在共享内容池选择内容", true);
    location.hash = "content";
    return;
  }
  await refresh();
  const pools = await api("/pools");
  if (!pools.length) {
    toast("请先创建号池并配置人设", true);
    location.hash = "pools";
    return;
  }
  const starts = Date.now() + 15 * 60000,
    ends = starts + 14 * 86400000;
  openPanel(
    "创建推广活动",
    `<form id="campaign-form"><p>已选择 ${poolState.selected.size} 条内容。</p><div class="field-grid"><div class="full"><label>活动名称<input name="name" required maxlength="160"></label></div><div class="full"><label for="select-pool_id">使用号池</label><select id="select-pool_id" name="pool_id">${pools.map((p) => `<option value="${p.id}">${esc(p.name)} · ${p.accounts.length} 个账号</option>`).join("")}</select></div><div><label>开始时间<input name="starts_at" type="datetime-local" required value="${isoLocal(starts)}"></label></div><div><label>结束时间<input name="ends_at" type="datetime-local" required value="${isoLocal(ends)}"></label></div>${listField("活动主题 / 查询（可留空）", "queries")}${listField("允许语言", "languages", ["en", "zh"])}${listField("必须包含的关键词", "required_keywords")}${listField("禁止关键词", "blocked_keywords")}<div><label>活动每日额度<input name="daily" type="number" min="1" max="100000" value="100" required></label></div><div><label>活动每小时额度<input name="hourly" type="number" min="1" max="10000" value="20" required></label></div><div><label>每批执行项<input name="batch_size" type="number" min="1" max="100" value="50" required></label></div><div><label>账号最小间隔（秒）<input name="min_interval" type="number" min="0" value="120" required></label></div><div><label>最低置信度<input name="min_confidence" type="number" min="0.5" max="1" step="0.05" value="0.8" required></label></div></div><p class="help">输入时间使用浏览器本地时区，预览统一显示北京时间；额度按 UTC 自然日计算。分配预览会列出无法匹配或无法按时执行的内容。真实账号间隔至少120秒。</p><div class="form-actions"><button>创建并规划分配</button></div></form>`,
  );
  $("#campaign-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    busy(e.submitter, async () => {
      const value = {
        name: f.get("name"),
        pool_id: f.get("pool_id"),
        opportunity_ids: [...poolState.selected],
        batch_size: Number(f.get("batch_size")),
        limit: {
          daily: Number(f.get("daily")),
          hourly: Number(f.get("hourly")),
        },
        policy: {
          queries: lines(f.get("queries")),
          languages: lines(f.get("languages")),
          required_keywords: lines(f.get("required_keywords")),
          blocked_keywords: lines(f.get("blocked_keywords")),
          starts_at: new Date(f.get("starts_at")).toISOString(),
          ends_at: new Date(f.get("ends_at")).toISOString(),
          min_interval: Number(f.get("min_interval")),
          min_confidence: Number(f.get("min_confidence")),
          daily_limit: Math.min(100, Number(f.get("daily"))),
          ordering_mode: "independent_accounts",
          pacing_scope: "account",
        },
      };
      const campaign = await api("/campaigns", "POST", value);
      $("#panel").close();
      location.hash = `campaigns/${campaign.id}`;
      try {
        await api(`/campaigns/${campaign.id}/plan`, "POST", {
          expected_revision: campaign.revision,
        });
      } finally {
        await renderCampaign(campaign.id);
      }
    });
  };
}

async function renderCampaign(id, offset = 0) {
  const c = await api(`/campaigns/${id}?offset=${offset}&limit=50`);
  const editable = ["draft", "planned"].includes(c.state);
  const ai = await api("/drafts/status");
  if (location.hash !== "#campaigns/" + id) return;
  $("#main").innerHTML =
    heading(
      esc(c.name),
      "准备分配、草稿和排期，点击开始发送后执行。",
      `<div class="actions"><a href="#campaigns">全部活动</a>${editable ? '<button class="secondary" id="plan-campaign">重新规划</button>' : ""}${c.state === "planned" ? `<button class="secondary" id="generate-drafts" ${ai.configured ? "" : "disabled"}>AI生成本页前20条草稿</button><button id="materialize-campaign">创建执行批次</button>` : ""}</div>`,
    ) +
    `<div class="stats"><span class="stat"><strong>${c.config.opportunity_ids.length}</strong>候选内容</span><span class="stat"><strong>${(c.summary.planned || 0) + (c.summary.batched || 0)}</strong>已分配</span><span class="stat"><strong>${c.summary.unassigned || 0}</strong>未分配</span><span class="stat"><strong>${c.execution_summary.confirmed || 0}</strong>确认发布</span></div><p class="help">已排期 ${c.execution_summary.scheduled || 0} · 结果未知 ${c.execution_summary.unknown || 0} · 待人工 ${c.execution_summary.manual_attention || 0} · 版本 ${c.revision}。各批按账号分组，账号之间可并发，同账号保持串行。</p>` +
    `<section class="surface padded mt-20"><h2>账号与人设效果</h2><p class="help">按活动使用的人设快照汇总。缺少完整反馈覆盖时，回复率显示不可统计。</p><div class="table-wrap"><table><thead><tr><th>账号 / 人设</th><th>计划分配</th><th>确认发布</th><th>已观察到回复</th><th>回复率</th></tr></thead><tbody>${c.accounts.map((a) => `<tr><td>${esc(a.label)}<span class="sub">${esc(a.role || "未指定角色")} · v${a.profile_version || 0}</span></td><td>${a.planned}</td><td>${a.confirmed}</td><td>${a.observed_replies || 0}</td><td>${a.reply_rate === null || a.reply_rate === undefined ? "不可统计" : (a.reply_rate * 100).toFixed(1) + "%"}</td></tr>`).join("")}</tbody></table></div><p class="help">${ai.configured ? "AI草稿服务已配置，可按页生成，生成结果仍需审核。" : "草稿使用导入正文或人设模板；配置AI草稿服务后可按页生成。"}</p></section>` +
    (c.tasks.length
      ? `<section class="surface padded"><h2>执行批次</h2><p class="help">打开批次查看账号、正文和时间，点击开始发送即可执行。</p><div class="table-wrap"><table><thead><tr><th>批次</th><th>任务阶段</th><th>状态</th></tr></thead><tbody>${c.tasks.map((t) => `<tr><td><a href="#tasks/${t.id}">${esc(t.name)}</a><span class="sub">${esc(t.id)}</span></td><td>${esc(phases[t.phase])}</td><td>${badge(t.state)}</td></tr>`).join("")}</tbody></table></div><div class="actions mt-14"><button class="secondary compact" id="pause-campaign" ${["cancelled", "completed"].includes(c.state) ? "disabled" : ""}>${c.state === "paused" ? "恢复活动" : "暂停活动"}</button><button class="danger compact" id="cancel-campaign" ${["cancelled", "completed"].includes(c.state) ? "disabled" : ""}>取消活动</button></div></section>`
      : "") +
    `<section class="surface mt-20"><div class="section-header"><h2>分配与草稿</h2><span>${offset + 1}–${Math.min(offset + 50, c.assignment_total)} / ${c.assignment_total}</span></div><div class="table-wrap"><table><thead><tr><th>内容 / 账号</th><th>理由</th><th>精确草稿与时间</th><th>执行</th></tr></thead><tbody>${c.assignments.map((a) => `<tr><td>${esc(a.source_payload.title || a.source_payload.external_id)}<span class="sub">${esc(c.accounts.find((x) => x.account_id === a.account_id)?.label || "未分配")}</span><span class="sub content-wrap">${esc(a.source_payload.target)}</span></td><td>${a.reasons.map((r) => `<span class="sub">${esc(r)}</span>`).join("")}</td><td>${a.payload ? `<details><summary>查看草稿 · 人设 v${a.payload.profile_version}</summary><div class="item-text">${esc(a.payload.text || "无需正文")}</div></details><span class="sub">${fmt(a.payload.scheduled_at)} UTC+08:00</span>${c.state === "planned" ? `<button class="text-button" data-edit-assignment="${a.id}">编辑草稿</button>` : ""}` : "—"}</td><td>${a.payload ? `<span class="badge ${a.payload.execution_mode === "manual" ? "amber" : "blue"}">${a.payload.execution_mode === "manual" ? "人工处理" : "自动执行"}</span>` : "—"}${a.item_status ? `<span class="sub">${badge(a.item_status)}</span>` : ""}</td></tr>`).join("")}</tbody></table></div></section><div class="form-actions"><button class="secondary" id="campaign-prev" ${offset === 0 ? "disabled" : ""}>上一页</button><button class="secondary" id="campaign-next" ${offset + 50 >= c.assignment_total ? "disabled" : ""}>下一页</button></div>`;
  $("#plan-campaign")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      await api(`/campaigns/${id}/plan`, "POST", {
        expected_revision: c.revision,
      });
      await renderCampaign(id);
    }),
  );
  $("#generate-drafts")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      const ids = c.assignments
        .filter(
          (a) =>
            a.status === "planned" &&
            !["like", "repost"].includes(a.payload.action),
        )
        .slice(0, 20)
        .map((a) => a.id);
      if (!ids.length) throw Error("本页没有需要生成正文的项目");
      await api(`/campaigns/${id}/drafts`, "POST", { assignment_ids: ids });
      await renderCampaign(id, offset);
      toast("AI草稿已保存，请逐项审核后创建批次");
    }),
  );
  $("#materialize-campaign")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      await api(`/campaigns/${id}/materialize`, "POST");
      await refresh();
      await renderCampaign(id);
    }),
  );
  $("#pause-campaign")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      await api(`/campaigns/${id}/state`, "POST", {
        state: c.state === "paused" ? "active" : "paused",
      });
      await renderCampaign(id, offset);
    }),
  );
  $("#cancel-campaign")?.addEventListener("click", (e) =>
    busy(e.target, async () => {
      await api(`/campaigns/${id}/state`, "POST", { state: "cancelled" });
      await renderCampaign(id, offset);
    }),
  );
  $("#campaign-prev").onclick = () =>
    renderCampaign(id, Math.max(0, offset - 50));
  $("#campaign-next").onclick = () => renderCampaign(id, offset + 50);
  document.querySelectorAll("[data-edit-assignment]").forEach(
    (b) =>
      (b.onclick = () => {
        const a = c.assignments.find((x) => x.id === b.dataset.editAssignment);
        openPanel(
          "编辑精确草稿",
          `<form id="assignment-form"><label>标题<input name="title" value="${esc(a.payload.title)}"></label><label for="assignment-text">正文</label><textarea id="assignment-text" name="text" rows="8">${esc(a.payload.text)}</textarea><div class="form-actions"><button>保存草稿</button></div></form>`,
        );
        $("#assignment-form").onsubmit = (e) => {
          e.preventDefault();
          const f = new FormData(e.target);
          busy(e.submitter, async () => {
            await api(`/assignments/${a.id}`, "PATCH", {
              text: f.get("text"),
              title: f.get("title"),
            });
            $("#panel").close();
            await renderCampaign(id, offset);
          });
        };
      }),
  );
}

async function executionSettingsForm() {
  const [config, health] = await Promise.all([
    api("/execution/settings"),
    api("/execution/pools"),
  ]);
  const channels = {
    "telegram.mtproto": "Telegram 用户账号",
    "x.edge": "X · Edge",
    "x.tabbit": "X · Tabbit",
    "x.playwright": "X · 独立浏览器",
    "x.browser_use": "X · 浏览器助手",
    "x.manual": "X · 人工处理",
    "reddit.manual": "Reddit · 人工处理",
    "instagram.graph": "Instagram · API",
    "instagram.manual": "Instagram · 人工评论",
    "discord.bot": "Discord · 机器人",
    "tiktok.organic": "TikTok · API",
  };
  const channelKeys = [
    ...new Set([
      ...Object.keys(channels),
      ...Object.keys(config.pool_workers || {}),
      ...health.jobs.map((j) => j.pool),
    ]),
  ];
  const channelRows = channelKeys
    .map((key) => {
      const jobs = health.jobs.filter((j) => j.pool === key);
      const pending = jobs
        .filter((j) => j.state === "pending")
        .reduce((n, j) => n + j.count, 0);
      const unknown = jobs
        .filter((j) => j.state === "unknown")
        .reduce((n, j) => n + j.count, 0);
      const running = health.workers.filter(
        (w) => w.pool === key && w.state === "running",
      ).length;
      return `<tr><td>${esc(channels[key] || key)}</td><td><input aria-label="${esc(channels[key] || key)} 并发" name="pool_${esc(key)}" type="number" min="1" max="50" value="${config.pool_workers?.[key] || 1}" required></td><td>${pending}</td><td>${unknown}</td><td>${running ? `${running} 个执行器在线` : "待命"}</td></tr>`;
    })
    .join("");
  const cloudLabel =
    health.cloud?.state === "connected" &&
    Date.now() / 1000 - health.cloud.at < 60
      ? "已连接"
      : health.cloud?.state === "not_configured"
        ? "尚未连接"
        : "连接待恢复";
  openPanel(
    "调度与额度",
    `<form id="execution-form"><div class="field-grid"><div><label>全局每日额度<input name="daily" type="number" min="1" max="100000" value="${config.global_limit.daily}" required></label></div><div><label>全局每小时额度<input name="hourly" type="number" min="1" max="10000" value="${config.global_limit.hourly}" required></label></div><div><label>同时执行的账号数<input name="max_workers" type="number" min="1" max="50" value="${config.max_workers}" required></label></div></div><p class="help">额度按UTC自然日计算。调高额度用于新任务；已启动任务继续受原额度快照约束。更严格的当前限制始终生效。</p><div class="table-wrap"><table><thead><tr><th>平台</th><th>并发账号</th><th>每日额度（可空）</th><th>每小时额度（可空）</th></tr></thead><tbody>${Object.keys(
      names,
    )
      .map(
        (p) =>
          `<tr><td>${esc(names[p])}</td><td><input aria-label="${names[p]} 并发账号" name="${p}_workers" type="number" min="1" max="50" value="${config.platform_workers[p] || config.max_workers}"></td><td><input aria-label="${names[p]} 每日额度" name="${p}_daily" type="number" min="1" max="100000" value="${config.platform_limits[p]?.daily || ""}"></td><td><input aria-label="${names[p]} 每小时额度" name="${p}_hourly" type="number" min="1" max="10000" value="${config.platform_limits[p]?.hourly || ""}"></td></tr>`,
      )
      .join(
        "",
      )}</tbody></table></div><h3>发送通道</h3><p class="help">云端：${cloudLabel} · 待补传回执：${health.cloud_receipts_pending} · 设置${health.cloud_settings_pending ? "等待同步" : "已同步"}。同一账号始终逐条处理；各通道还受上方平台及全局限制。结果不明的内容只核验，不重发；空闲执行器按需启动。</p><div class="table-wrap"><table><thead><tr><th>通道</th><th>并发上限</th><th>待处理</th><th>待核验</th><th>运行状态</th></tr></thead><tbody>${channelRows}</tbody></table></div><div class="form-actions"><button>保存调度设置</button></div></form>`,
  );
  $("#execution-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target),
      platform_limits = {},
      platform_workers = {};
    Object.keys(names).forEach((p) => {
      platform_workers[p] = Number(f.get(`${p}_workers`));
      if (f.get(`${p}_daily`) || f.get(`${p}_hourly`))
        platform_limits[p] = {
          daily: Number(f.get(`${p}_daily`)) || 100000,
          hourly: Number(f.get(`${p}_hourly`)) || 10000,
        };
    });
    busy(e.submitter, async () => {
      await api("/execution/settings", "PUT", {
        global_limit: {
          daily: Number(f.get("daily")),
          hourly: Number(f.get("hourly")),
        },
        max_workers: Number(f.get("max_workers")),
        platform_limits,
        platform_workers,
        pool_workers: Object.fromEntries(
          channelKeys.map((key) => [key, Number(f.get(`pool_${key}`))]),
        ),
        quota_timezone: "UTC",
      });
      $("#panel").close();
      toast("调度设置已保存");
    });
  };
}

async function collectContentForm() {
  await refresh();
  const accounts = state.accounts.filter(
    (a) => a.platform === "telegram" && !a.disabled,
  );
  if (!accounts.length) throw Error("请先登记 Telegram 账号");
  openPanel(
    "读取公开群消息",
    `<form id="collect-content-form"><label for="select-account_id">读取账号</label><select id="select-account_id" name="account_id">${accounts.map((a) => `<option value="${a.id}">${esc(a.label)} · ${a.mode === "simulation" ? "模拟" : "真实"}</option>`).join("")}</select><label>公开群目标<input name="target" placeholder="@public_group" required></label><label>最多读取条数<input name="limit" type="number" min="1" max="1000" value="200" required></label><p class="help">仅只读采集公开群文字，排除本账号消息，不入群或回复。采集内容的互动许可默认待核实，真实自动互动前需登记许可及证据。</p><div class="form-actions"><button>读取并导入内容池</button></div></form>`,
  );
  $("#collect-content-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    busy(e.submitter, async () => {
      const result = await api(
        `/accounts/${f.get("account_id")}/content`,
        "POST",
        { target: f.get("target"), limit: Number(f.get("limit")) },
      );
      result.ids.forEach((id) => poolState.selected.add(id));
      $("#panel").close();
      await renderContent(0);
      toast(
        `读取完成，新增 ${result.imported} 条${result.truncated ? "；仍有更多群消息" : ""}`,
      );
    });
  };
}

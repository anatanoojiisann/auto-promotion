"use strict";
const growthState = { inboxOffset: 0 };
const growthMetric = (v) => (v === null || v === undefined ? "未接入" : esc(v));
const pct = (v) =>
  v === null || v === undefined
    ? "未设置或缺少数据"
    : (v * 100).toFixed(1) + "%";
const growthInput = (label, name, value = "", type = "text", attrs = "") =>
  `<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" ${attrs}></label>`;

async function renderGrowth() {
  const [goals, ready] = await Promise.all([
    api("/growth/goals"),
    api("/growth/readiness"),
  ]);
  const checks = [
    ["Telegram 可用账号", ready.telegram_usable, "accounts"],
    ["账号角色待补齐", ready.profiles_missing.length, "pools"],
    ["号池", ready.pools, "pools"],
    ["有资格证据的内容", ready.content_with_reviewed_evidence, "content"],
    ["采集来源", ready.feed_count, "feeds"],
  ];
  $("#main").innerHTML =
    heading(
      "业务增长",
      "把互动、下载、首次生成和真实付费放进同一观察计划。",
      '<button id="new-growth">设置业务目标</button>',
    ) +
    `<div class="stats growth-stats">${checks.map(([label, count, route]) => `<a class="stat" href="#${route}"><strong>${count}</strong>${label}</a>`).join("")}</div>` +
    `<div class="surface padded"><h2>运行与数据准备</h2><p>${ready.global_stop ? "外发已停止" : "外发停止开关已解除"} · ${ready.sender_configured ? "后台发送已配置" : "后台发送未配置"} · ${ready.read_scheduler_configured ? "只读采集调度已配置" : "只读采集调度未配置"}</p><p class="help">资格证据数量不代表可直接发送，最终仍按账号、目标、额度和启动状态检查。X、Reddit 使用人工互动清单。</p>${ready.profiles_missing.length ? `<p>待配置角色：${esc(ready.profiles_missing.join("、"))}</p>` : ""}${
      Object.keys(ready.task_blockers).length
        ? `<details><summary>未结束任务的执行阻碍</summary><ul>${Object.entries(
            ready.task_blockers,
          )
            .map(([message, n]) => `<li>${esc(message)}：${n} 个任务</li>`)
            .join("")}</ul></details>`
        : ""
    }<div class="actions"><a href="#tasks">检查任务</a><a href="#feeds">管理只读采集</a><a href="#inbox">处理回复</a></div></div>` +
    (goals.length
      ? `<div class="surface table-wrap mt-20"><table><thead><tr><th>业务目标</th><th>观察周期</th><th>下载目标</th><th>付费目标</th></tr></thead><tbody>${goals.map((g) => `<tr><td><a href="#growth/${g.id}">${esc(g.payload.name)}</a><span class="sub">${g.payload.mode === "live" ? "真实业务" : "模拟验证"} · ${esc(g.payload.countries.join(" / ") || "全部地区")}</span></td><td>${fmt(g.payload.starts_at)}<span class="sub">至 ${fmt(g.payload.ends_at)}</span></td><td>${g.payload.downloads_target ?? "未设置"}</td><td>${g.payload.paid_target ?? "未设置"}</td></tr>`).join("")}</tbody></table></div>`
      : empty(
          "尚未设置业务目标",
          "先确定观察周期和验收指标，再关联推广活动与渠道数据。",
        ));
  $("#new-growth").onclick = () => growthGoalForm();
}

async function growthGoalForm(goal) {
  const campaigns = await api("/campaigns");
  const p = goal?.payload || {};
  openPanel(
    goal ? "编辑业务目标" : "设置业务目标",
    `<form id="growth-goal-form"><div class="field-grid"><div class="full">${growthInput("目标名称", "name", p.name, "text", "required")}</div><div class="full">${growthInput("官方 App Store 应用链接", "app_store_url", p.app_store_url, "url", `required ${goal ? "readonly" : ""}`)}</div><label>数据范围<select aria-label="数据范围" name="mode" ${goal ? "disabled" : ""}><option value="live" ${p.mode !== "simulation" ? "selected" : ""}>真实业务</option><option value="simulation" ${p.mode === "simulation" ? "selected" : ""}>模拟验证</option></select></label>${growthInput("国家／地区代码（留空为全部）", "countries", (p.countries || []).join(", "))}${growthInput("开始时间", "starts", isoLocal(p.starts_at ? new Date(p.starts_at).getTime() : Date.now()), "datetime-local", "required")}${growthInput("结束时间", "ends", isoLocal(p.ends_at ? new Date(p.ends_at).getTime() : Date.now() + 7 * 86400000), "datetime-local", "required")}${growthInput("观察期新增下载目标", "downloads_target", p.downloads_target ?? "", "number", "min=1")}${growthInput("观察期付费人数目标", "paid_target", p.paid_target ?? "", "number", "min=1")}${growthInput("每日互动目标", "daily_interactions", p.daily_interactions ?? "", "number", "min=1")}${growthInput("可接受获客成本（最小货币单位）", "max_cac_minor", p.max_cac_minor ?? "", "number", "min=0")}${growthInput("币种", "currency", p.currency || "USD", "text", "required pattern=[A-Z]{3}")}<fieldset class="full"><legend>关联推广活动</legend>${campaigns.map((c) => `<label class="checkbox-label"><input type="checkbox" name="campaign_ids" value="${c.id}" ${(p.campaign_ids || []).includes(c.id) ? "checked" : ""}>${esc(c.name)}</label>`).join("") || "暂无活动，可稍后关联"}</fieldset></div><p class="help">时间使用浏览器本地时区，保存为明确 UTC 时间。空白指标表示尚未设置；保存目标不会修改账号额度或触发发送。</p><div class="form-actions"><button>保存目标</button></div></form>`,
  );
  $("#growth-goal-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    busy(e.submitter, async () => {
      const v = {
        name: f.get("name"),
        app_store_url: f.get("app_store_url"),
        mode: p.mode || f.get("mode"),
        starts_at: new Date(f.get("starts")).toISOString(),
        ends_at: new Date(f.get("ends")).toISOString(),
        timezone: p.timezone || "Asia/Shanghai",
        countries: lines(f.get("countries")).map((c) => c.toUpperCase()),
        currency: f.get("currency"),
        campaign_ids: f.getAll("campaign_ids"),
      };
      for (const key of [
        "downloads_target",
        "paid_target",
        "daily_interactions",
        "max_cac_minor",
      ])
        v[key] = f.get(key) === "" ? null : Number(f.get(key));
      const saved = await api(
        "/growth/goals" + (goal ? "/" + goal.id : ""),
        goal ? "PUT" : "POST",
        v,
      );
      $("#panel").close();
      location.hash = "growth/" + saved.id;
      await renderGrowthGoal(saved.id);
    });
  };
}

async function renderGrowthGoal(id) {
  const r = await api(`/growth/goals/${id}`),
    p = r.goal.payload,
    f = r.event_funnel;
  $("#main").innerHTML =
    heading(
      esc(p.name),
      `${p.mode === "simulation" ? "模拟验证" : "真实业务"} · ${fmt(p.starts_at)} 至 ${fmt(p.ends_at)}`,
      '<div class="actions"><a href="#growth">全部目标</a><button class="secondary" id="edit-growth">编辑目标</button><button class="secondary" id="refresh-growth">刷新数据</button></div>',
    ) +
    `<div class="stats growth-stats"><div class="stat"><strong>${growthMetric(r.store_metrics.first_downloads)}</strong>商店渠道首次下载<span class="sub">目标完成 ${pct(r.progress.store_downloads)}</span></div><div class="stat"><strong>${f.first_download}</strong>已观察首次下载</div><div class="stat"><strong>${f.first_generation}</strong>已观察首次生成</div><div class="stat"><strong>${f.first_charge}</strong>已观察首次付费<span class="sub">目标完成 ${pct(r.progress.observed_paid)}</span></div></div>` +
    `<div class="surface padded"><h2>试用与收入</h2><p>已观察试用 ${f.trial_start} 人 · 已到期 ${r.mature_trials} 人 · 尚未到期 ${r.immature_trials} 人 · 到期后扣费 ${r.mature_trial_paid} 人</p><p>已观察到期转付费比例：${pct(r.observed_trial_to_paid_rate)} · 已观察获客成本：${growthMetric(r.observed_cac_minor)} ${esc(p.currency)} 最小单位</p>${
      Object.entries(r.revenue_by_currency)
        .map(
          ([currency, m]) =>
            `<p>${esc(currency)}：扣费 ${m.charges_minor} · 退款 ${m.refunds_minor} · 净收入 ${m.net_minor}（最小货币单位）</p>`,
        )
        .join("") || '<p class="help">尚无扣费／退款事件。</p>'
    }<p class="help">${r.data_status === "not_connected" ? "尚未导入业务数据。" : "数据来自已导入证据，覆盖尚未确认完整。"}商店汇总和匿名用户事件分别展示；未知来源下载 ${r.unattributed_event_downloads} 人，不匹配下载队列的事件 ${r.events_without_cohort_download} 条，地区不符或缺少地区的事件 ${r.excluded_country_events} 条。缺失数据不当作零转化。</p><div class="actions"><button class="secondary" id="import-events">导入应用事件</button><button class="secondary" id="import-metrics">导入商店指标与成本</button></div></div>` +
    `<div class="surface padded mt-20"><div class="section-header"><h2>渠道与账号归因</h2><button id="new-channel">添加渠道</button></div>${r.channels.length ? `<div class="table-wrap"><table><thead><tr><th>渠道</th><th>渠道链接</th><th>观察下载 / 生成 / 付费</th></tr></thead><tbody>${r.channels.map((c) => `<tr><td>${esc(c.payload.name)}<span class="sub">${esc(c.payload.campaign_token)}</span><span class="sub">${esc(c.id)}</span></td><td>${c.url ? `<a class="content-wrap" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">${esc(c.url)}</a><button class="text-button" data-copy-channel="${c.id}">复制链接</button>` : "等待 App Store Connect 的提供商令牌"}</td><td>${r.channel_funnel[c.id].first_download} / ${r.channel_funnel[c.id].first_generation} / ${r.channel_funnel[c.id].first_charge}</td></tr>`).join("")}</tbody></table></div>` : empty("尚无渠道", "每个渠道可关联活动和账号，使用不同的渠道标记。")}</div>`;
  $("#edit-growth").onclick = () => growthGoalForm(r.goal);
  $("#main .stats").insertAdjacentHTML(
    "afterend",
    `<div class="surface padded mt-20"><h2>互动与成本目标</h2><p>${p.campaign_ids.length ? `关联活动已核验投递：期内 ${r.interactions.confirmed_in_window} 条 · 今天 ${r.interactions.confirmed_today} 条 · 日目标进度 ${pct(r.interactions.daily_target_progress)}` : "尚未关联推广活动，互动结果暂未纳入此目标。"}</p><p>可接受获客成本：${p.max_cac_minor === null ? "未设置" : `${p.max_cac_minor} ${esc(p.currency)} 最小单位`} · ${r.cac_within_target === null ? "缺少可比较数据" : r.cac_within_target ? "已观察成本在目标内" : "已观察成本超出目标"}</p></div>`,
  );
  $("#refresh-growth").onclick = (e) =>
    busy(e.target, () => renderGrowthGoal(id));
  $("#new-channel").onclick = () => channelForm(r);
  $("#import-events").onclick = () => growthImportForm(r, "events");
  $("#import-metrics").onclick = () => growthImportForm(r, "store-metrics");
  document.querySelectorAll("[data-copy-channel]").forEach(
    (b) =>
      (b.onclick = (e) =>
        busy(e.target, async () => {
          await navigator.clipboard.writeText(
            r.channels.find((c) => c.id === b.dataset.copyChannel).url,
          );
          toast("渠道链接已复制");
        })),
  );
}

async function channelForm(report) {
  await refresh();
  const accounts = state.accounts.filter(
    (a) => a.mode === report.goal.payload.mode,
  );
  openPanel(
    "添加渠道",
    `<form id="growth-channel-form">${growthInput("渠道名称", "name", "", "text", "required")}${growthInput("渠道标记（英文、数字、下划线或连字符）", "campaign_token", "", "text", "required maxlength=30 pattern=[A-Za-z0-9_\\-]+")}${growthInput("提供商令牌（从 App Store Connect 获取，可稍后新建渠道）", "provider_token", "", "text", "pattern=[0-9]*")}<label>推广活动<select name="campaign_id"><option value="">未关联</option>${report.goal.payload.campaign_ids.map((id) => `<option value="${esc(id)}">${esc(id)}</option>`).join("")}</select></label><label>渠道账号<select name="account_id"><option value="">未关联</option>${accounts.map((a) => `<option value="${a.id}">${esc(a.label)}</option>`).join("")}</select></label><p class="help">提供商令牌须来自真实 App Store Connect 链接。渠道记录保留原始标记；它不会改写已启动的正文，也不提供逐用户安装归因。</p><div class="form-actions"><button>保存渠道</button></div></form>`,
  );
  $("#growth-channel-form").onsubmit = (e) => {
    e.preventDefault();
    busy(e.submitter, async () => {
      await api(
        `/growth/goals/${report.goal.id}/channels`,
        "POST",
        Object.fromEntries(new FormData(e.target)),
      );
      $("#panel").close();
      await renderGrowthGoal(report.goal.id);
    });
  };
}

function growthImportForm(report, kind) {
  const channelId = report.channels[0]?.id || "";
  const example =
    kind === "events"
      ? [
          {
            source: "app-backend",
            event_id: "unique-event-001",
            subject_key: "anonymous-user-key",
            kind: "first_download",
            channel_id: channelId,
            occurred_at: new Date().toISOString(),
            country: "US",
            evidence_reference: "backend-export-row-001",
          },
        ]
      : [
          {
            channel_id: channelId,
            day: new Date().toLocaleDateString("sv-SE", {
              timeZone: report.goal.payload.timezone,
            }),
            country: "US",
            source: "App Store Connect export",
            evidence_reference: "export-file-row",
            first_downloads: null,
            product_page_views: null,
            spend_minor: null,
            currency: report.goal.payload.currency,
          },
        ];
  openPanel(
    kind === "events" ? "导入应用事件" : "导入商店指标与成本",
    `<form id="growth-import-form"><label>本机 JSON / CSV 文件<input id="growth-import-file" type="file" accept=".json,.csv"></label><label>数据内容<textarea id="growth-import-json" rows="13" required>${esc(JSON.stringify(example, null, 2))}</textarea></label><p class="help">应用事件支持 first_download、first_generation、trial_start、first_charge、refund。subject_key 使用稳定匿名标识，不填写手机号或邮箱。试用需 trial_ends_at；扣费／退款需 transaction_id、amount_minor、currency。金额使用币种最小单位。商店指标用日期、渠道和地区保存，可重新导入修订；空白或 null 表示未知。应用事件按来源编号去重，冲突整批拒绝。</p><div class="form-actions"><button>核对并导入</button></div></form>`,
  );
  $("#growth-import-file").onchange = async (e) => {
    try {
      const file = e.target.files[0];
      if (!file) return;
      const text = await file.text();
      $("#growth-import-json").value = file.name.toLowerCase().endsWith(".csv")
        ? JSON.stringify(
            csvItems(text).map((row) => {
              for (const key of [
                "amount_minor",
                "product_page_views",
                "first_downloads",
                "spend_minor",
              ])
                if (row[key] !== undefined)
                  row[key] = row[key] === "null" ? null : Number(row[key]);
              return row;
            }),
            null,
            2,
          )
        : text;
    } catch (error) {
      toast(error.message, true);
    }
  };
  $("#growth-import-form").onsubmit = (e) => {
    e.preventDefault();
    busy(e.submitter, async () => {
      const v = JSON.parse($("#growth-import-json").value);
      const result = await api(
        `/growth/goals/${report.goal.id}/${kind}`,
        "POST",
        { items: Array.isArray(v) ? v : v.items },
      );
      $("#panel").close();
      await renderGrowthGoal(report.goal.id);
      toast(
        `导入完成：${result.imported ?? result.saved} 条${result.duplicates ? `，重复 ${result.duplicates} 条` : ""}`,
      );
    });
  };
}

async function renderInbox(offset = growthState.inboxOffset) {
  growthState.inboxOffset = offset;
  const data = await api(`/growth/inbox?offset=${offset}&limit=100`);
  const states = {
    open: "待处理",
    reviewing: "审核中",
    dismissed: "已结束",
    responded: "已回复并核验",
  };
  $("#reply-count").textContent = data.counts.open || "";
  $("#main").innerHTML =
    heading(
      "回复待办",
      "收到的回复关联原互动；后续回复进入新任务，点击开始发送后执行。",
      '<button class="secondary" id="refresh-inbox">刷新待办</button>',
    ) +
    `<div class="stats growth-stats">${Object.entries(states)
      .map(
        ([key, label]) =>
          `<div class="stat"><strong>${data.counts[key] || 0}</strong>${label}</div>`,
      )
      .join("")}</div>` +
    (data.items.length
      ? `<div class="surface table-wrap"><table><thead><tr><th>回复来源</th><th>原互动</th><th>处理状态</th><th>操作</th></tr></thead><tbody>${data.items.map((r) => `<tr><td>${esc(r.account_label)}<span class="sub">${esc(r.payload.target)} · 回复 ${esc(r.event_id.split(":").pop())}</span><span class="sub">${fmt(r.occurred_at)}</span></td><td><a href="#tasks/${r.task_id}">查看原任务</a><span class="sub content-wrap">${esc(r.payload.text.slice(0, 180))}</span></td><td>${states[r.state]}<span class="sub content-wrap">${esc(r.note)}</span>${r.followup_task_id ? `<a href="#tasks/${r.followup_task_id}">后续执行任务</a>` : ""}</td><td><button class="secondary compact" data-triage="${r.id}">处理</button><button class="text-button" data-followup="${r.id}">创建后续任务</button></td></tr>`).join("")}</tbody></table></div>`
      : empty(
          "尚无待办",
          "只读反馈采集发现他人直接回复后，会自动进入此列表。",
        )) +
    `<p class="help">目前自动反馈来自 Telegram。回复正文没有保存，处理前请在平台核对原消息。清单以外的新回复需要新任务；待办状态不授予发送权限。</p><div class="form-actions"><button id="inbox-prev" class="secondary" ${offset === 0 ? "disabled" : ""}>上一页</button><span>${data.total} 条</span><button id="inbox-next" class="secondary" ${offset + 100 >= data.total ? "disabled" : ""}>下一页</button></div>`;
  $("#refresh-inbox").onclick = (e) =>
    busy(e.target, () => renderInbox(offset));
  $("#inbox-prev").onclick = () => renderInbox(Math.max(0, offset - 100));
  $("#inbox-next").onclick = () => renderInbox(offset + 100);
  document
    .querySelectorAll("[data-triage]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          replyTriageForm(data.items.find((r) => r.id === b.dataset.triage))),
    );
  document
    .querySelectorAll("[data-followup]")
    .forEach(
      (b) =>
        (b.onclick = (e) =>
          busy(e.target, () =>
            followupForm(data.items.find((r) => r.id === b.dataset.followup)),
          )),
    );
}

function replyTriageForm(reply) {
  openPanel(
    "处理回复待办",
    `<form id="triage-form"><label>处理状态<select name="state">${Object.entries(
      {
        open: "待处理",
        reviewing: "审核中",
        dismissed: "结束本轮讨论",
        responded: "回复已核验投递",
      },
    )
      .map(
        ([v, t]) =>
          `<option value="${v}" ${v === reply.state ? "selected" : ""}>${t}</option>`,
      )
      .join(
        "",
      )}</select></label>${growthInput("后续任务编号", "followup_task_id", reply.followup_task_id || "")}<label>处理备注<textarea name="note" rows="4">${esc(reply.note)}</textarea></label><p class="help">标记已回复时，系统会检查后续任务中针对这条消息的已核验投递。</p><div class="form-actions"><button>保存处理状态</button></div></form>`,
  );
  $("#triage-form").onsubmit = (e) => {
    e.preventDefault();
    busy(e.submitter, async () => {
      await api(
        `/growth/inbox/${reply.id}`,
        "PATCH",
        Object.fromEntries(new FormData(e.target)),
      );
      $("#panel").close();
      await renderInbox();
    });
  };
}

async function followupForm(reply) {
  await refresh();
  const account = state.accounts.find((a) => a.id === reply.account_id);
  openPanel(
    "创建后续回复任务",
    `<form id="followup-form">${growthInput("任务名称", "name", "后续回复 · " + reply.payload.target, "text", "required")}${growthInput("允许语言（逗号分隔）", "languages", "en, zh", "text", "required")}${growthInput("开始时间", "starts", isoLocal(Date.now()), "datetime-local", "required")}${growthInput("结束时间", "ends", isoLocal(Date.now() + 86400000), "datetime-local", "required")}<p>${esc(account.label)} · ${esc(reply.payload.target)} · 回复消息 ${esc(reply.event_id.split(":").pop())}</p><p class="help">创建任务后编辑完整回复正文，再点击开始发送。目标与回复编号会带入编辑器；不会自动生成或发送回复。</p><div class="form-actions"><button>创建任务草稿</button></div></form>`,
  );
  $("#followup-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    busy(e.submitter, async () => {
      const task = await api(`/growth/inbox/${reply.id}/followup`, "POST", {
        name: f.get("name"),
        account_ids: [account.id],
        policy: {
          starts_at: new Date(f.get("starts")).toISOString(),
          ends_at: new Date(f.get("ends")).toISOString(),
          languages: lines(f.get("languages")),
          target_allowlist: [reply.payload.target],
          max_joins: 0,
          min_interval: account.min_interval,
          daily_limit: 1,
        },
      });
      $("#panel").close();
      location.hash = "tasks/" + task.id;
      await renderTask(task.id);
    });
  };
}

async function renderFeeds() {
  const feeds = await api("/growth/feeds");
  $("#main").innerHTML =
    heading(
      "内容采集来源",
      "按计划只读采集指定公开 Telegram 群，自动去重入池。",
      '<div class="actions"><a href="#content">共享内容池</a><button class="secondary" id="poll-feeds">运行到期采集</button><button id="new-feed">添加来源</button></div>',
    ) +
    (feeds.length
      ? `<div class="surface table-wrap"><table><thead><tr><th>公开群</th><th>采集间隔</th><th>最近结果</th><th>操作</th></tr></thead><tbody>${feeds.map((f) => `<tr><td>${esc(f.payload.target)}<span class="sub">${f.payload.enabled ? "已启用" : "已停用"}</span></td><td>${f.payload.interval_seconds / 60} 分钟<span class="sub">下次 ${fmt(f.next_at)}</span></td><td>${esc({ waiting: "等待采集", complete: "读取完成", needs_check: "读取未完成，请检查账号" }[f.status] || f.status)}<span class="sub">${fmt(f.last_at)} · 新增 ${f.result.imported ?? "—"}${f.result.truncated ? " · 读取范围已截断" : ""}</span></td><td><button class="secondary compact" data-feed="${f.id}">编辑</button></td></tr>`).join("")}</tbody></table></div>`
      : empty("尚无采集来源", "添加来源并启用后，后台只读调度会定期采集。")) +
    '<p class="help">默认停用。开启后仅采集公开群，不入群或回复。消息入池后的互动许可仍需核对。后台只读调度和电脑需要持续运行；采集失败会保留原因并等待下一轮。</p>';
  $("#new-feed").onclick = () => feedForm();
  $("#poll-feeds").onclick = (e) =>
    busy(e.target, async () => {
      const r = await api("/growth/feeds/poll", "POST");
      await renderFeeds();
      toast(`完成 ${r.completed} 个到期来源`);
    });
  document
    .querySelectorAll("[data-feed]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          feedForm(feeds.find((f) => f.id === b.dataset.feed))),
    );
}

async function feedForm(feed) {
  await refresh();
  const p = feed?.payload || {};
  openPanel(
    feed ? "编辑采集来源" : "添加采集来源",
    `<form id="feed-form"><label>读取账号<select name="account_id" required>${state.accounts
      .filter((a) => a.platform === "telegram" && !a.disabled)
      .map(
        (a) =>
          `<option value="${a.id}" ${a.id === p.account_id ? "selected" : ""}>${esc(a.label)} · ${a.mode}</option>`,
      )
      .join(
        "",
      )}</select></label>${growthInput("公开群", "target", p.target || "", "text", "required placeholder=@public_group")}${growthInput("间隔（分钟）", "interval", (p.interval_seconds || 1800) / 60, "number", "required min=15 max=1440")}${growthInput("每次最多读取条数", "limit", p.limit || 100, "number", "required min=1 max=1000")}<label class="checkbox-label"><input name="enabled" type="checkbox" ${p.enabled ? "checked" : ""}>启用定时只读采集</label><div class="form-actions"><button>保存来源</button></div></form>`,
  );
  $("#feed-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    busy(e.submitter, async () => {
      await api(
        "/growth/feeds" + (feed ? "/" + feed.id : ""),
        feed ? "PUT" : "POST",
        {
          account_id: f.get("account_id"),
          target: f.get("target"),
          interval_seconds: Number(f.get("interval")) * 60,
          limit: Number(f.get("limit")),
          enabled: f.has("enabled"),
        },
      );
      $("#panel").close();
      await renderFeeds();
    });
  };
}

function contentReviewForm(opportunity) {
  openPanel(
    "核对内容资格与文案",
    `<form id="content-review-form"><label>内容、上下文、许可和正文<textarea id="content-review-json" rows="17" required>${esc(JSON.stringify(opportunity.payload, null, 2))}</textarea></label><p class="help">允许互动需要填写真实许可及证据。平台、目标与原消息标识不能更换；已生成执行批次保留原快照，更新只用于后续计划。</p><div class="form-actions"><button>保存核对结果</button></div></form>`,
  );
  $("#content-review-form").onsubmit = (e) => {
    e.preventDefault();
    busy(e.submitter, async () => {
      await api(`/opportunities/${opportunity.id}`, "PUT", {
        expected_revision: opportunity.revision,
        payload: JSON.parse($("#content-review-json").value),
      });
      $("#panel").close();
      await renderContent();
    });
  };
}

let inboxPolling = false;
setInterval(async () => {
  if (inboxPolling || document.hidden) return;
  inboxPolling = true;
  try {
    const data = await api("/growth/inbox?limit=1");
    $("#reply-count").textContent = data.counts.open || "";
  } catch {
  } finally {
    inboxPolling = false;
  }
}, 30000);

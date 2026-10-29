"use strict";
const hfStateNames = {
  DISCOVERED: "待判定 / P3 候补",
  REVIEW_PENDING: "等待人工选择",
  QUEUED: "等待生成条件",
  GENERATING: "生成中",
  READY: "等待发送",
  PUBLISHING: "提交中",
  VERIFYING: "待核验 · 禁止补发",
  PUBLISHED: "真实发布已核验",
  SIMULATED: "模拟完成",
  CLOSED: "已关闭",
};
const hfMetric = (v) => (v === null || v === undefined ? "未知" : esc(v));
const hfActions = {
  x_api_builtin_backend_required:
    "将运行服务的 AP_X_BACKEND 改为 builtin；高频路线不支持 legacy/Xtf。",
  x_api_account_required:
    "选择 connection_type=x_api 的官方 API 账号；浏览器连接不能用于此自动路线。",
  x_api_token_missing:
    "按官方接入说明由操作者补齐本机私有配置；现有浏览器登录表单不能录入 X API 凭据。不要将凭据发到聊天。",
  x_api_user_id_missing: "补齐官方 API 账号 user_id，并用账号检查核对身份。",
  scan_source_unavailable:
    "全部所选账号须支持官方 API；首个账号用于搜索，其余账号也不能是浏览器连接。",
  x_api_read_cooldown:
    "搜索限流或额度限制仍在退避期；等待重试时间并核对 X 用量，不会自动付费。",
  XAPIRateLimited:
    "X 返回限流或用量上限；已保存只读搜索退避，等待后仍需核对用量。",
  XAPIAuthenticationRejected:
    "搜索凭据无效或已失效；由操作者核对已有 API 凭据。",
  XAPIAccessDenied:
    "搜索读取权限不足；核对应用及套餐的 recent search 访问范围，不能改走浏览器。",
  XAPISearchRejected: "搜索参数或资源被拒绝；核对查询与时间窗。",
  XAPISearchResponseInvalid:
    "搜索响应结构或数量不完整，本轮不计空轮、不推进该查询水位。",
  XAPIPartialSearch: "X 返回部分失败，本轮覆盖不完整，不计健康空轮。",
  model_connection_unavailable:
    "配置 AP_DRAFT_ENDPOINT 与 AP_DRAFT_MODEL；非空配置不等于外部连接已验证。",
  model_endpoint_invalid: "修正模型端点 URL 格式。",
  model_identifier_missing: "配置非空 AP_DRAFT_MODEL。",
  worker_unavailable:
    "按运维说明启动带 worker 的工作台；保存策略不会自行启动后台。",
  worker_unverified: "后台存活检查未完成，先核对服务状态。",
};
const hfReason = (code) =>
  hfActions[code] ? `${code}：${hfActions[code]}` : code;
const hfMinutes = (seconds) => Number((seconds / 60).toFixed(2));
const hfDurations = (values) =>
  values.map((value) => Number(value.toFixed(3))).join(" / ");
const hfLink = (url, label = "查看原帖 ↗") =>
  /^https:\/\/(?:www\.)?(?:x|twitter)\.com\//.test(url || "")
    ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`
    : `<span>${esc(url || "—")}</span>`;

async function renderInteractions(id) {
  if (!id) {
    const policies = await api("/interactions");
    $("#main").innerHTML =
      heading(
        "高频互动",
        "持续发现真实需求，P0/P1 自动执行，P2/P3 由你选择。",
        '<div class="actions"><button id="hf-sim" class="secondary">运行隔离模拟</button><button id="hf-new" class="secondary">新建原策略</button><button id="hf-new-joint">联合 P0/P1 模板</button></div>',
      ) +
      (policies.length
        ? `<div class="surface padded">${policies.map((p) => `<div class="log-row"><a href="#interactions/${esc(p.id)}"><strong>${esc(p.config.project_id)} / ${esc(p.config.policy_key)}</strong></a><span>${p.config.enabled ? "已启用" : "已停用"} · ${p.config.run_mode === "live" ? "正式路线" : p.config.run_mode === "discovery_only" ? "只读发现" : "dry-run"} · v${p.version} · 空轮 ${p.empty_streak}</span></div>`).join("")}</div>`
        : empty(
            "尚未创建策略",
            "默认每 10 分钟扫描；首条 P2 打开五小时批次。先保存范围与账号，再启用。",
          ));
    $("#hf-new").onclick = () => hfSettings();
    $("#hf-new-joint").onclick = async () =>
      hfJointSettings(
        null,
        await api("/interactions/defaults?profile=joint_p0_p1"),
      );
    $("#hf-sim").onclick = (e) => busy(e.target, hfSimulation);
    return;
  }
  const [p, candidates, processing] = await Promise.all([
    api(`/interactions/${id}`),
    api(`/interactions/${id}/candidates`),
    api(`/interactions/${id}/processing-tasks`),
  ]);
  const cfg = p.config;
  const readiness = p.readiness;
  const delivery = p.delivery_summary;
  const joint = cfg.strategy_profile === "joint_p0_p1";
  const readOnly = cfg.run_mode === "discovery_only";
  const count = (state) =>
    p.counts.filter((r) => r.state === state).reduce((n, r) => n + r.count, 0);
  $("#main").innerHTML =
    heading(
      `${cfg.project_id} · 高频互动`,
      `版本 ${p.version} · 计数版本 ${p.count_epoch} · ${cfg.timezone}`,
      '<div class="actions"><button class="secondary" id="hf-back">全部策略</button><button class="secondary" id="hf-refresh">刷新</button><button class="secondary" id="hf-settings">设置</button><button id="hf-scan">扫描一次</button></div>',
    ) +
    `<div class="banner ${cfg.enabled ? "" : "warning"}"><div><strong>${cfg.enabled ? "策略已启用" : "策略已停用"} · ${cfg.run_mode === "live" ? "正式执行路线" : readOnly ? "只读发现 · 回复逐条启动" : "dry-run · 不真实发布"}</strong><p>${readOnly ? "仅保存观察到的公开帖子；你在任务中心逐条启动具体回复。浏览器结果会标记覆盖不完整。" : `P0/P1 条件满足即自动执行；${joint ? "本策略仅记录 P2/P3，不推广。" : "P2/P3 必须采用。"}`}${p.global_stop ? "当前全局停止阻止新提交。" : ""} ${state.overview.worker ? "后台调度运行中。" : readOnly ? "此页面不运行发送器；只读扫描可由独立定时任务推进。" : "当前服务未开启后台调度，扫描将进入持久队列，需启动后台服务。"}</p></div><button id="hf-toggle" class="secondary">${cfg.enabled ? "停用策略" : "启用策略"}</button></div>` +
    `<div class="banner ${readiness.ready ? "" : "warning"}" id="hf-readiness"><div><strong>启用前检查：${readiness.ready ? "本机配置前提齐备 · 外部连接未验证" : "存在阻塞"}</strong><p>来源 ${esc(readiness.source)} · 覆盖 ${esc(readiness.coverage)} · 后台 ${esc(readiness.worker)}${readiness.retry_at ? ` · 搜索重试不早于 ${fmt(readiness.retry_at)}` : ""}<br>${readiness.blockers.map((r) => esc(hfReason(r))).join(" · ") || "发送仍需逐目标资格、事实、额度和原通道核验。"}</p>${cfg.scan.source === "imported_results" ? '<button id="hf-import" class="secondary">导入已有采集候选</button><p>仅已有结果人工导入，覆盖不完整；不运行浏览器实时扫描，不计完整空轮/P3。</p>' : ""}</div></div>` +
    `<div class="stats" id="hf-delivery"><div class="stat">真实核验发布 <strong>${delivery.confirmed}</strong></div><div class="stat">模拟 <strong>${delivery.simulated}</strong></div><div class="stat">未发送过窗 <strong>${delivery.missed}</strong></div><div class="stat">结果未知 · 禁止补发 <strong>${delivery.unknown}</strong></div></div>` +
    p.warnings.map((w) => `<p class="banner warning">${esc(w)}</p>`).join("") +
    (readOnly && cfg.policy_key.endsWith("_observer")
      ? `<div class="banner"><div><strong>分账号优先级：P0 → P1 → P2</strong><p>本账号每轮核查真实相关帖子，逐级寻找并最多准备一条草稿回复任务。下次计划扫描 ${fmt(p.next_scan_at)}；回复仍由你逐条启动。</p></div></div>`
      : readOnly && p.readonly_fallback
        ? `<div class="banner ${p.readonly_fallback.due ? "warning" : ""}"><div><strong>P2/P3 人工补充轮 · ${p.readonly_fallback.due ? "到期，等待核查" : "等待两小时观察窗口"}</strong><p>下次检查 ${fmt(p.readonly_fallback.due_at)} · 当前可复核帖子 ${p.readonly_fallback.shortlist.length} 条。只代表已观察到的浏览器结果；找到合适帖子后仅准备一条草稿任务，回复仍由你逐条启动。</p></div></div>`
        : "") +
    `<div class="stats">${joint ? `<div class="stat"><strong>${cfg.scan.schedule_mode === "random_interval" ? `${hfMinutes(cfg.scan.min_interval_seconds)}–${hfMinutes(cfg.scan.max_interval_seconds)}` : hfMinutes(cfg.scan.interval_seconds)} 分钟</strong>计划扫描间隔<br><small>从实际启动时间计时</small></div><div class="stat"><strong>${processing.length}</strong>互动处理任务</div>` : `<div class="stat"><strong>${p.empty_streak} / ${cfg.fallback.empty_rounds_required}</strong>连续完整空轮<br><small>${hfMinutes(cfg.scan.interval_seconds)} 分钟 × ${cfg.fallback.empty_rounds_required} 轮 ≈ ${p.fallback_after_seconds / 60} 分钟</small></div><div class="stat"><strong>${count("REVIEW_PENDING")}</strong>等待人工选择</div>`}<div class="stat"><strong>${count("GENERATING")}</strong>生成中</div><div class="stat"><strong>${count("VERIFYING")}</strong>待核验</div></div>` +
    `<div class="two-col mt-20"><div class="surface padded"><h2>扫描与通知</h2><p>最近完整成功：${fmt(p.last_complete_at)}<br>下次扫描：<span id="hf-next-scan">${fmt(p.next_scan_at)}</span><br>延期：${esc(p.deferred_reason || "无")} ${p.deferred_until ? fmt(p.deferred_until) : ""}<br>当前搜索账号：${esc(p.rotation.current_search_account_id || "无活动采集")}<br>最近启动账号：${esc(p.rotation.last_started_account_id || "尚未启动")}<br>上轮搜索账号：${esc(p.rotation.previous_search_account_id || "尚无上轮")}<br>严格切换：${cfg.search_accounts?.require_different_from_previous && cfg.strategy_profile === "joint_p0_p1" ? "开启" : "未启用"}<br>${joint ? "" : `P3 上次评估：${fmt(p.last_p3_at)}<br>P3 冷却：${cfg.fallback.evaluation_cooldown_seconds / 60} 分钟<br>`}通知通道：${esc(p.notification_channel)}</p>${p.batches
      .filter((b) => ["open", "sealed"].includes(b.state))
      .map(
        (b) =>
          `<p>批次 ${esc(b.state)} · 打开 ${fmt(b.opened_at)} · 到期 ${fmt(b.due_at)}</p>`,
      )
      .join(
        "",
      )}</div><div class="surface padded"><h2>${readOnly ? "只读连接" : "执行条件"}</h2>${p.effective_accounts.map((a) => `<p><strong>${esc(a.label)}</strong> · ${a.mode === "simulation" ? "模拟" : "真实账号"}<br>${esc(a.capabilities.transport)} · ${esc(readOnly ? a.capabilities.discovery : a.capabilities.publishing)}<br>有效额度 ${a.hourly_limit}/小时，${a.daily_limit == null ? "本地账号每日不限" : a.daily_limit + "/天"}，间隔 ${a.min_interval} 秒</p>`).join("") || "未选择账号"}${readOnly ? "" : `<p>媒体：${p.media.configured ? "已配置，按需检查预算" : esc(p.media.reason)}；未配置收费预算时只继续文字流程。</p>`}${p.block_reasons.map((r) => `<span class="chip">${esc(r.block_reason)} × ${r.count}</span>`).join(" ")}</div></div>` +
    (joint
      ? '<label class="mt-20">当前页按产品筛选<select id="hf-product-filter"><option value="">全部产品</option><option value="aigram">AIgram</option><option value="aurax">Aurax AI</option></select></label>'
      : "") +
    hfProcessingCards(processing) +
    `<section class="surface padded mt-24"><div class="section-header"><div><h2>${readOnly ? "公开帖子观察" : "候选选择"}</h2><p class="help">每页显示最近 100 条。${readOnly ? "仅记录浏览器可见内容；到任务中心逐条准备并明确启动回复。" : joint ? "P0/P1 自动处理，无需采用；可勾选未提交项以跳过或暂缓。" : "仅采用勾选项；未选项继续等待，直至有效期结束。"}</p></div></div><div class="actions" ${readOnly ? "hidden" : ""}><button id="hf-adopt" ${joint ? "hidden" : ""}>采用所选</button><button class="secondary" id="hf-skip">跳过所选</button><button class="secondary" id="hf-defer">暂缓所选</button></div><div class="hf-candidates">${
      candidates
        .map((c) => {
          const selectable =
            c.state === "REVIEW_PENDING" ||
            (joint &&
              ["P0", "P1"].includes(c.priority) &&
              ["QUEUED", "GENERATING", "READY"].includes(c.state));
          const editable =
            selectable ||
            (cfg.strategy_profile === "joint_p0_p1" &&
              ["QUEUED", "GENERATING", "READY"].includes(c.state));
          return `<article class="hf-candidate" data-hf-products="${esc(JSON.stringify(c.matched_product_ids.length ? c.matched_product_ids : c.classification.matched_product_ids?.length ? c.classification.matched_product_ids : [...new Set(c.discoveries.flatMap((d) => d.product_ids))]))}"><div class="section-header"><label><input type="checkbox" data-hf-select="${esc(c.id)}" data-version="${c.version}" ${selectable ? "" : "disabled"}> <strong>${esc(c.priority || "待判定")}</strong> · ${esc(readOnly && c.state === "DISCOVERED" ? "待人工查看" : hfStateNames[c.state] || c.state)}</label><span>v${c.version} · ${hfLink(c.source.url)}</span></div><p class="content-wrap">${esc(c.source.text)}</p>${c.source.context ? `<details><summary>必要上下文</summary><p class="content-wrap">${esc(c.source.context)}</p></details>` : ""}<p>${esc(c.classification.priority_reason || c.block_reason || "等待分类")}<br><span class="help">事实依据：${esc((c.classification.verified_fact_ids || []).join(", "))} · ${esc(c.classification.product_fit)}</span></p><p>${readOnly ? "发现账号" : "建议账号"}：${esc(p.effective_accounts.find((a) => a.account_id === c.owner_account_id)?.label || c.owner_account_id)}<br>原帖 ${fmt(c.source_event_at)} · 发现 ${fmt(c.first_seen_at)} · 截止 ${fmt(c.expires_at)}${c.defer_until ? ` · 暂缓至 ${fmt(c.defer_until)}` : ""}</p><div class="banner"><div><strong>回复草稿 / 贡献方案</strong><p class="content-wrap">${esc(c.draft || c.classification.contribution || "尚无草稿")}</p><p class="help">媒体：${esc(c.media_plan.reason || c.classification.recommended_media || "none")} · ${esc(c.media_plan.source || "文字优先")}</p></div></div>${c.media_plan.assets?.map((a) => `<p>${esc(a.filename)} · 来源 ${esc(c.media_plan.source)} · ${c.media_plan.real_product_output ? "有真实产品输出证据" : "外部示意素材"}</p>`).join("") || ""}<p class="help">资格 ${esc(c.channel_eligibility)}${c.block_reason ? ` · 受阻：${esc(readOnly && c.block_reason === "manual_review_required" ? "等待人工核对" : c.block_reason)}` : ""}${c.closed_reason ? ` · ${esc(c.closed_reason)}` : ""}</p>${c.external_reference ? `<p>${hfLink(c.external_reference, c.state === "SIMULATED" ? "模拟结果" : "真实发布记录 ↗")}</p>` : ""}<div class="actions">${editable ? `<button class="secondary compact" data-hf-edit="${esc(c.id)}">编辑正文与账号</button>` : ""}${c.state === "VERIFYING" ? `<button class="secondary compact" data-hf-verify="${esc(c.id)}">只读核验</button>` : ""}${c.task_id ? `<a href="#tasks/${esc(c.task_id)}">发送任务与回执</a>` : ""}${readOnly ? `<a href="#tasks">在任务中心准备回复</a>` : ""}</div></article>`;
        })
        .join("") ||
      empty("尚无候选", "运行扫描后在这里查看真实后端保存的发现与判定。")
    }</div></section>` +
    `<section class="surface padded mt-24"><h2>持久通知</h2>${p.notices.map((n) => `<details><summary>${esc(n.notice_type)} · ${esc(n.state)} · ${fmt(n.created_at)} · 修订 ${n.revision}</summary><p>${esc(n.error || n.message_id || "等待发送")}</p>${n.snapshot.map((s) => `<p><strong>${esc(s.priority || "故障")}</strong> ${hfLink(s.url)}<br>${esc(s.need || "")}<br>${esc(s.reason || "")}<br>${esc(s.draft || n.body)}<br>截止 ${fmt(s.expires_at)}</p>`).join("")}</details>`).join("") || "暂无通知"}</section>` +
    `<section class="surface padded mt-24"><h2>运行与来源覆盖</h2><div class="table-wrap"><table><thead><tr><th>开始 / 结束</th><th>轮次 / 结果</th><th>获取 / 重复 / 新高优先级</th><th>覆盖与分类</th></tr></thead><tbody>${p.runs.map((r) => `<tr><td>${fmt(r.started_at)}<br>${fmt(r.finished_at)}</td><td>${r.seq} · ${esc(r.state)}<br>搜索账号 ${esc(r.search_account_id || "历史记录")}<br>抽样 ${esc(r.sampled_interval_seconds || "—")} 秒 · ${esc(r.trigger_type)}<br>计划 ${fmt(r.scheduled_at)}<br>下一次 ${fmt(r.next_scan_at)}<br>${esc(r.error || "")}</td><td>${r.fetched} / ${r.duplicates} / ${r.new_high}</td><td>${r.queries.map((q) => `${esc((q.product_ids ? JSON.parse(q.product_ids).join(" / ") + " · " : "") + (q.expression || q.query))}：${q.complete ? "采集完整" : "未完整"}，${q.pages} 页 ${esc(q.error || "")}`).join("<br>")}<br>${r.classification_counts.map((x) => `${esc(x.priority || x.state)} ${x.count}`).join(" / ")}</td></tr>`).join("")}</tbody></table></div></section>` +
    `<section class="surface padded mt-24"><h2>结果与费用</h2><p>发现 ${p.metrics.candidate_count} · 有效 P0/P1 ${p.metrics.qualified_p0_p1} · 重复 ${p.metrics.duplicates} · 已分配 ${p.metrics.assigned} · 待分配 ${p.metrics.waiting_assignment} · 已发布 ${p.metrics.published} · 待核验 ${p.metrics.verifying}。<br>实际启动间隔（秒）：${esc(hfDurations(p.metrics.actual_start_intervals_seconds) || "尚无相邻轮次")}<br>确认到建单（秒）：${esc(hfDurations(p.metrics.qualified_to_task_seconds) || "尚无处理任务")} · 确认到分配（秒）：${esc(hfDurations(p.metrics.qualified_to_assignment_seconds) || "尚无分配")}</p><p>采用率 ${p.metrics.manual_adoption_rate === null ? "未知" : (p.metrics.manual_adoption_rate * 100).toFixed(1) + "%"} · 等待人工而失效 ${p.metrics.expired_waiting_review} 条。观看、访问、安装与首次生成：未接通归因，保留未知。</p><div class="table-wrap"><table><thead><tr><th>调用</th><th>次数</th><th>耗时 ms</th><th>预估费用</th><th>实际费用</th></tr></thead><tbody>${p.usage.map((u) => `<tr><td>${esc(u.kind)}</td><td>${u.calls}</td><td>${hfMetric(u.duration_ms)}</td><td>${hfMetric(u.estimated_cost)} ${esc(cfg.generation.currency)}</td><td>${hfMetric(u.actual_cost)} ${esc(cfg.generation.currency)}</td></tr>`).join("")}</tbody></table></div></section>`;
  if ($("#hf-import"))
    $("#hf-import").onclick = (e) => busy(e.target, () => hfImportSources(p));
  $("#hf-scan").disabled = cfg.scan.source === "imported_results";
  $("#hf-back").onclick = () => {
    location.hash = "interactions";
  };
  if (joint)
    $("#hf-product-filter").onchange = (event) => {
      document.querySelectorAll("[data-hf-products]").forEach((el) => {
        el.hidden =
          !!event.target.value &&
          !JSON.parse(el.dataset.hfProducts).includes(event.target.value);
      });
    };
  $("#hf-refresh").onclick = (e) =>
    busy(e.target, () => renderInteractions(id));
  $("#hf-settings").onclick = () => hfSettings(p);
  $("#hf-toggle").onclick = (e) =>
    busy(e.target, async () => {
      await api("/interactions", "POST", {
        config: { ...cfg, enabled: !cfg.enabled },
        expected_version: p.version,
      });
      await renderInteractions(id);
    });
  $("#hf-scan").onclick = (e) =>
    busy(e.target, async () => {
      const result = await api(`/interactions/${id}/scan`, "POST", undefined, {
        "Idempotency-Key": crypto.randomUUID(),
      });
      toast(
        result.reason
          ? `等待扫描条件：${result.reason}`
          : result.coalesced
            ? "已合并到待执行触发"
            : "扫描已进入持久队列",
      );
      await renderInteractions(id);
    });
  const versions = () =>
    Object.fromEntries(
      [...document.querySelectorAll("[data-hf-select]:checked")].map((el) => [
        el.dataset.hfSelect,
        Number(el.dataset.version),
      ]),
    );
  for (const action of ["adopt", "skip", "defer"]) {
    $(`#hf-${action}`).onclick = (e) =>
      busy(e.target, async () => {
        const selected = versions();
        if (!Object.keys(selected).length)
          throw new Error("请先勾选要处理的候选");
        if (action === "defer") {
          openPanel(
            "暂缓所选",
            '<form id="hf-defer-form"><label>稍后查看时间（北京时间）<input type="datetime-local" name="until" required></label><p class="help">不延长有效期，不自动发布。稍后时间必须早于所有所选候选的截止时间。</p><button type="submit">保存暂缓</button></form>',
          );
          $("#hf-defer-form").onsubmit = (event) => {
            event.preventDefault();
            busy(event.submitter, async () => {
              await api("/interactions/actions", "POST", {
                action,
                versions: selected,
                defer_until:
                  Date.parse(event.target.elements.until.value + "+08:00") /
                  1000,
              });
              await renderInteractions(id);
              closePanel();
            });
          };
          return;
        }
        await api("/interactions/actions", "POST", {
          action,
          versions: selected,
        });
        toast(
          action === "adopt" ? "所选候选已进入执行条件检查" : "所选候选已跳过",
        );
        await renderInteractions(id);
      });
  }
  document.querySelectorAll("[data-hf-edit]").forEach(
    (b) =>
      (b.onclick = () => {
        const c = candidates.find((c) => c.id === b.dataset.hfEdit);
        openPanel(
          "编辑候选",
          `<form id="hf-edit-form"><label>发送账号<select name="account">${p.effective_accounts
            .filter(
              (a) =>
                cfg.strategy_profile !== "joint_p0_p1" ||
                ((c.priority !== "P0" ||
                  a.account_id === c.conversation_owner_account_id) &&
                  (
                    cfg.products.find(
                      (p) => p.product_id === c.primary_product_id,
                    )?.execution_account_ids || []
                  ).includes(a.account_id)),
            )
            .map(
              (a) =>
                `<option value="${esc(a.account_id)}" ${a.account_id === c.owner_account_id ? "selected" : ""}>${esc(a.label)}</option>`,
            )
            .join(
              "",
            )}</select></label><label>回复正文<textarea name="text" rows="7" required>${esc(c.draft || c.classification.contribution)}</textarea></label><p class="help">${cfg.strategy_profile === "joint_p0_p1" ? "保存后继续自动处理" : "编辑后需重新采用"}；生成器会保留你编辑的正文。</p><button type="submit">保存编辑</button></form>`,
        );
        $("#hf-edit-form").onsubmit = (event) => {
          event.preventDefault();
          busy(event.submitter, async () => {
            await api("/interactions/actions", "POST", {
              action: "edit",
              versions: { [c.id]: c.version },
              text: event.target.elements.text.value,
              account_id: event.target.elements.account.value,
            });
            await renderInteractions(id);
            closePanel();
          });
        };
      }),
  );
  document.querySelectorAll("[data-hf-verify]").forEach(
    (b) =>
      (b.onclick = (e) =>
        busy(e.target, async () => {
          const c = candidates.find((c) => c.id === b.dataset.hfVerify);
          await api("/interactions/actions", "POST", {
            action: "verify",
            versions: { [c.id]: c.version },
          });
          toast("只读核验已入队");
        })),
  );
  for (const candidate of candidates) {
    const pending = candidate.generations.filter(
      (g) =>
        g.kind !== "text" &&
        ["pending", "unknown", "submitting"].includes(g.state),
    );
    if (!pending.length) continue;
    const article = document
      .querySelector(`[data-hf-select="${candidate.id}"]`)
      .closest("article");
    article
      .querySelector(".actions")
      .insertAdjacentHTML(
        "beforeend",
        `<button class="secondary compact" data-hf-media-query="${esc(candidate.id)}">只读核验素材任务</button>`,
      );
    article.querySelector("[data-hf-media-query]").onclick = (event) =>
      busy(event.target, async () => {
        await api("/interactions/actions", "POST", {
          action: "verify_media",
          versions: { [candidate.id]: candidate.version },
        });
        toast("查询原素材任务已入队，不会重新下单");
      });
  }
}

async function hfSettings(policy) {
  if (policy?.config.strategy_profile === "joint_p0_p1")
    return hfJointSettings(policy, policy.config);
  const cfg = policy?.config || (await api("/interactions/defaults"));
  const accounts = await api("/interactions/accounts");
  const input = (label, name, value, type = "text") =>
    `<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" required></label>`;
  openPanel(
    policy ? "高频互动设置" : "新建高频互动策略",
    `<form id="hf-settings-form"><div class="field-grid">${input("项目标识", "project", cfg.project_id)}${input("受众 / 查询策略标识", "key", cfg.policy_key)}<label>执行模式<select name="mode"><option value="dry_run" ${cfg.run_mode === "dry_run" ? "selected" : ""}>dry-run · 模拟提交</option><option value="live" ${cfg.run_mode === "live" ? "selected" : ""}>正式执行 · P0/P1 自动</option></select></label><label>启用<select name="enabled"><option value="false" ${!cfg.enabled ? "selected" : ""}>停用</option><option value="true" ${cfg.enabled ? "selected" : ""}>启用</option></select></label><div class="full"><label>发送账号 · 原选择顺序首个账号共用官方 API 扫描</label>${accounts.map((a) => `<label class="hf-account"><input type="checkbox" name="accounts" value="${esc(a.id)}" data-api-ready="${a.mode === "simulation" || (a.capabilities.transport === "x_api" && !(a.capabilities.blockers || []).length)}" ${cfg.account_ids.includes(a.id) ? "checked" : ""}> ${esc(a.label)} · ${a.mode === "simulation" ? "模拟" : "真实"} · ${esc(a.capabilities.transport)}${(a.capabilities.blockers || []).map((r) => `<small>${esc(hfReason(r))}</small>`).join("")}</label>`).join("") || '<p class="help">先在账号中心登记 X 账号。</p>'}</div><label class="full">查询（每行一条）<textarea name="queries" rows="4">${esc(cfg.queries.join("\n"))}</textarea></label><label class="full">受众范围<textarea name="audience" rows="2">${esc(cfg.audience)}</textarea></label><label>发现来源<select name="source"><option value="x_api" ${cfg.scan.source !== "imported_results" ? "selected" : ""}>官方 API 定时扫描</option><option value="imported_results" ${cfg.scan.source === "imported_results" ? "selected" : ""}>已有采集结果人工导入 · 覆盖不完整</option></select></label>${input("扫描间隔（秒）", "interval", cfg.scan.interval_seconds, "number")}${input("首次入队等待下限（秒）", "delayMin", cfg.publishing.enqueue_delay_min_seconds ?? 600, "number")}${input("首次入队等待上限（秒）", "delayMax", cfg.publishing.enqueue_delay_max_seconds ?? 900, "number")}${input("P2 等待（秒）", "batch", cfg.review.batch_wait_seconds, "number")}${input("P3 连续空轮", "rounds", cfg.fallback.empty_rounds_required, "number")}${input("P3 冷却（秒）", "cooldown", cfg.fallback.evaluation_cooldown_seconds, "number")}</div><p id="hf-ready-result" class="help"></p><p id="hf-calculation" class="help"></p><p class="help">P0/P1：资格满足后自动生成与发布，无逐条审批。P2/P3：人工采用后才执行。通知静默与扫描、发布时段独立。</p><label><input type="checkbox" name="apply">将等待时间修改应用到已有未封存批次（从原打开时间重算）</label><details><summary>高级配置、事实依据、有效期与预算</summary><p class="help">全部时间单位为秒。null 额度继承当前账号；收费媒体预算未配置时禁用付费生成。产品事实须填写来源、核实时间与有效期。渠道资格来源于后端证据，不能在这里强行设为允许。</p><textarea id="hf-advanced" name="advanced" rows="24" spellcheck="false">${esc(JSON.stringify(cfg, null, 2))}</textarea></details><div class="form-actions"><button type="submit">保存策略</button></div></form>`,
  );
  const form = $("#hf-settings-form");
  const updateAccounts = () => {
    for (const field of form.querySelectorAll('[name="accounts"]'))
      field.disabled =
        form.elements.source.value === "x_api" &&
        field.dataset.apiReady === "false" &&
        !field.checked;
  };
  form.elements.source.onchange = updateAccounts;
  for (const field of form.querySelectorAll('[name="accounts"]'))
    field.onchange = updateAccounts;
  updateAccounts();
  if (policy) {
    form.elements.project.readOnly = true;
    form.elements.key.readOnly = true;
  }
  const calc = () => {
    $("#hf-calculation").textContent =
      `${Number(form.elements.interval.value) / 60} 分钟 × ${Number(form.elements.rounds.value)} 轮 ≈ ${(Number(form.elements.interval.value) * Number(form.elements.rounds.value)) / 60} 分钟；扫描间隔不代表发布次数。`;
  };
  form.elements.interval.oninput = calc;
  form.elements.rounds.oninput = calc;
  calc();
  form.onsubmit = (e) => {
    e.preventDefault();
    busy(e.submitter, async () => {
      const config = JSON.parse(form.elements.advanced.value);
      Object.assign(config, {
        project_id: form.elements.project.value.trim(),
        policy_key: form.elements.key.value.trim(),
        run_mode: form.elements.mode.value,
        enabled: form.elements.enabled.value === "true",
        account_ids: (() => {
          const selected = [
            ...form.querySelectorAll('[name="accounts"]:checked'),
          ].map((e) => e.value);
          return [
            ...cfg.account_ids.filter((id) => selected.includes(id)),
            ...selected.filter((id) => !cfg.account_ids.includes(id)),
          ];
        })(),
        audience: form.elements.audience.value,
        queries: form.elements.queries.value
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean),
      });
      config.scan.source = form.elements.source.value;
      config.publishing.enqueue_delay_min_seconds = Number(
        form.elements.delayMin.value,
      );
      config.publishing.enqueue_delay_max_seconds = Number(
        form.elements.delayMax.value,
      );
      config.scan.interval_seconds = Number(form.elements.interval.value);
      config.review.batch_wait_seconds = Number(form.elements.batch.value);
      config.fallback.empty_rounds_required = Number(
        form.elements.rounds.value,
      );
      config.fallback.evaluation_cooldown_seconds = Number(
        form.elements.cooldown.value,
      );
      const readiness = await api("/interactions/readiness", "POST", {
        config,
      });
      $("#hf-ready-result").textContent = readiness.blockers.length
        ? "本机前提阻塞：" + readiness.blockers.map(hfReason).join(" · ")
        : "本机配置前提齐备；模型/API真实连接及发送资格仍需验证。";
      const saved = await api("/interactions", "POST", {
        config,
        expected_version: policy?.version || 0,
        apply_timing_to_open_batches: form.elements.apply.checked,
      });
      closePanel();
      location.hash = "interactions/" + saved.id;
      await renderInteractions(saved.id);
    });
  };
}

async function hfSimulation() {
  const result = await api("/interactions/simulate", "POST");
  openPanel(
    "隔离模拟验收",
    `<p>使用独立临时数据库、模拟账号和可替换时钟；真实发布与收费媒体调用均为 ${result.external_calls}。</p><div class="log-row"><strong>验收结果</strong><span>${result.passed ? "通过" : "存在未通过项"}</span></div>${Object.entries(
      result.checks,
    )
      .map(([name, value]) => `<p>${value ? "✓" : "✕"} ${esc(name)}</p>`)
      .join(
        "",
      )}<details><summary>实际模拟状态与证据</summary><pre class="content-wrap">${esc(JSON.stringify(result.evidence, null, 2))}</pre></details>`,
  );
}

async function hfImportSources(policy) {
  const options = await api(`/interactions/${policy.id}/import-options`);
  if (!options.length)
    throw new Error(
      "本项目无已采集的公开 X 候选；不会自动启动浏览器。可通过 imports 接口导入带原帖时间的已有公开结果。",
    );
  openPanel(
    "已有采集结果人工导入",
    `<form id="hf-import-form"><p>复用本项目已保存的候选；补齐原帖公开元数据。不启动浏览器，不声明完整覆盖，不取得发送资格。</p><label>已有候选<select name="candidate">${options.map((o) => `<option value="${esc(o.id)}">${esc(o.target)} · ${esc(o.text.slice(0, 100))}</option>`).join("")}</select></label><label>原帖作者公开 ID<input name="author" required maxlength="100"></label><label>原帖发布时间（UTC）<input type="datetime-local" name="eventTime" required></label><label>已核对上下文<textarea name="context" maxlength="50000"></textarea></label><label>上下文状态<select name="contextStatus"><option value="missing">尚未核实</option><option value="complete">已核对完整公开上下文</option><option value="inaccessible">无法访问</option></select></label><button type="submit">导入已有结果</button></form>`,
  );
  $("#hf-import-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.target;
    busy(e.submitter, async () => {
      await api(`/interactions/${policy.id}/imports`, "POST", {
        sources: [
          {
            legacy_candidate_id: form.elements.candidate.value,
            author_id: form.elements.author.value.trim(),
            source_event_at:
              Date.parse(form.elements.eventTime.value + "Z") / 1000,
            context: form.elements.context.value,
            context_status: form.elements.contextStatus.value,
          },
        ],
      });
      closePanel();
      await renderInteractions(policy.id);
      toast("已保存候选并排队分类；覆盖不完整，发送资格仍由后端核验。");
    });
  };
}

function hfProcessingCards(tasks) {
  const names = {
    TASK_CREATED: "已建单",
    ASSIGNED: "已分配",
    WAITING_ACCOUNT: "等待执行账号",
    WAITING_FACTS: "等待答案事实",
    WAITING_CAPABILITY: "等待执行资格",
    ...hfStateNames,
  };
  return `<section class="surface padded mt-24" id="hf-processing"><h2>互动处理任务</h2><p class="help">建单、分配、生成与发布分别记录；已分配表示已有执行归属。</p>${
    tasks
      .map((t) => {
        const c = t.candidate;
        return `<article class="hf-candidate" data-processing-id="${esc(t.id)}" data-hf-products="${esc(JSON.stringify(t.matched_product_ids))}"><strong>${esc(t.priority)} · ${esc(names[t.state] || t.state)}</strong><p>${esc(t.id)} · ${esc(names[t.assignment_status] || t.assignment_status)}<br>主产品 ${esc(t.primary_product_id || "待核实")} · 匹配 ${esc(t.matched_product_ids.join(" / "))}</p><p>${hfLink(c.source.url)} ${esc(c.source.text)}</p><p>${esc(c.classification.priority_reason || "")}<br>原文依据：${esc((c.classification.intent_evidence || []).map((e) => (typeof e === "string" ? e : e.quote)).join(" / "))}</p><p>发现账号 ${esc([...new Set(c.discoveries.map((d) => d.discovery_account_id))].join(" / "))}<br>会话账号 ${esc(t.conversation_owner_account_id || "不适用")}<br>执行账号 ${esc(t.execution_account_id || "待分配")}<br>确认 ${fmt(t.qualified_at)} · 建单 ${fmt(t.created_at)} · 分配 ${fmt(t.assigned_at)}</p><p class="content-wrap">${esc(c.draft || c.classification.contribution || "等待草稿")}</p><p>资格 ${esc(t.channel_eligibility)} · ${esc(t.block_reason || c.closed_reason || "无受阻原因")}</p>${t.task_id ? `<a href="#tasks/${esc(t.task_id)}">发送任务 ${esc(t.task_id)}</a>` : "发送任务：尚未建立"}${c.external_reference ? `<p>${hfLink(c.external_reference, "发布回执")}</p>` : ""}</article>`;
      })
      .join("") || "暂无合格处理任务"
  }</section>`;
}

async function hfJointSettings(policy, cfg) {
  const accounts = (await api("/accounts")).filter((a) => a.platform === "x");
  const input = (label, name, value, type = "text") =>
    `<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" required></label>`;
  const checkboxes = (name, ids) =>
    accounts
      .map(
        (a) =>
          `<label class="hf-account"><input type="checkbox" name="${name}" value="${esc(a.id)}" ${ids.includes(a.id) ? "checked" : ""}> ${esc(a.label)} · ${a.mode === "simulation" ? "模拟" : "真实"}</label>`,
      )
      .join("");
  const languagePriority = (product, language) =>
    `<label>${language === "ko" ? "韩文" : "日文"}优先账号<select name="preferred-${esc(product.product_id)}-${language}"><option value="">按产品默认顺序</option>${accounts
      .filter((a) => product.execution_account_ids.includes(a.id))
      .map(
        (a) =>
          `<option value="${esc(a.id)}" ${(product.preferred_execution_account_ids_by_language?.[language] || [])[0] === a.id ? "selected" : ""}>${esc(a.label)}</option>`,
      )
      .join("")}</select></label>`;
  openPanel(
    "AIgram + Aurax 联合 P0/P1 设置",
    `<form id="hf-joint-form"><p>每轮覆盖两产品。只自动处理 P0/P1，P2/P3 不派单、不回退。</p><div class="field-grid">${input("现有推广项目标识", "project", cfg.project_id)}${input("策略标识", "key", cfg.policy_key)}<label>执行模式<select name="mode"><option value="discovery_only" ${cfg.run_mode === "discovery_only" ? "selected" : ""}>只读发现 · 回复逐条启动</option><option value="dry_run" ${cfg.run_mode === "dry_run" ? "selected" : ""}>dry-run</option><option value="live" ${cfg.run_mode === "live" ? "selected" : ""}>正式路线</option></select></label><label>启用<select name="enabled"><option value="false" ${!cfg.enabled ? "selected" : ""}>停用</option><option value="true" ${cfg.enabled ? "selected" : ""}>启用</option></select></label><label>扫描模式<select name="schedule"><option value="random_interval" ${cfg.scan.schedule_mode === "random_interval" ? "selected" : ""}>随机间隔</option><option value="fixed_interval" ${cfg.scan.schedule_mode === "fixed_interval" ? "selected" : ""}>固定间隔</option></select></label>${input("固定间隔（秒）", "interval", cfg.scan.interval_seconds, "number")}${input("最小间隔（秒）", "minimum", cfg.scan.min_interval_seconds, "number")}${input("最大间隔（秒）", "maximum", cfg.scan.max_interval_seconds, "number")}${input("长期中断阈值（秒）", "longgap", cfg.scan.long_gap_reset_seconds, "number")}</div><p id="hf-joint-average" class="help"></p><p>当前已排定：${policy ? fmt(policy.next_scan_at) : "首次启用后尽快执行"}。普通保存保留当前计划；新参数在下一次抽样生效。</p><fieldset><legend>搜索账号与固定轮换</legend>${checkboxes("search", cfg.search_accounts.account_ids)}<label>轮换顺序（账号 ID，每行一个）<textarea name="order" rows="4">${esc(cfg.search_accounts.account_ids.join("\n"))}</textarea></label><label><input type="checkbox" name="strict" ${cfg.search_accounts.require_different_from_previous ? "checked" : ""}>相邻新轮次必须切换平台身份</label><p class="help">勾选账号按这里的顺序轮换；新增账号接在末尾。严格切换至少需要两个已核验的不同身份。</p></fieldset>${cfg.products
      .map(
        (p) =>
          `<fieldset data-product="${esc(p.product_id)}"><legend>${esc(p.product_id === "aigram" ? "AIgram" : "Aurax AI")}</legend><label><input type="checkbox" name="enabled-${esc(p.product_id)}" ${p.enabled ? "checked" : ""}>启用产品</label>${input("受众语言（逗号分隔）", `languages-${p.product_id}`, p.languages.join(","))}${input("事实版本", `knowledge-${p.product_id}`, p.knowledge_version)}<label>目标平台 / 发行版本<input name="platforms-${esc(p.product_id)}" value="${esc(p.target_platforms.join(","))}" placeholder="例如 android"><input name="versions-${esc(p.product_id)}" value="${esc(p.target_versions.join(","))}" placeholder="已验证的发行版本"></label><label>公开需求查询（每行一条）<textarea name="queries-${esc(p.product_id)}" rows="4">${esc(
            p.query_groups
              .filter((q) => q.enabled)
              .map((q) => q.expression)
              .join("\n"),
          )}</textarea></label><p>该产品允许的执行账号</p>${checkboxes(`execution-${p.product_id}`, p.execution_account_ids)}${["ko", "ja"].map((language) => languagePriority(p, language)).join("")}<details><summary>查询来源、预算与产品事实</summary><p class="help">查询组保留稳定 ID、版本和来源。修改这里后，下方事实随产品保存。未核实或未发行的能力不参与自动回复。</p><label>查询组（JSON）<textarea name="groups-${esc(p.product_id)}" rows="8">${esc(JSON.stringify(p.query_groups, null, 2))}</textarea></label><label>已验证事实（JSON）<textarea name="facts-${esc(p.product_id)}" rows="10">${esc(JSON.stringify(p.facts, null, 2))}</textarea></label></details></fieldset>`,
      )
      .join(
        "",
      )}<details><summary>全部配置、读取预算与有效期</summary><textarea name="advanced" rows="22">${esc(JSON.stringify(cfg, null, 2))}</textarea></details><div class="form-actions"><button type="submit">保存策略</button>${policy ? '<button type="button" class="secondary" id="hf-reschedule">按已保存参数重排下一次</button>' : ""}</div></form>`,
  );
  const form = $("#hf-joint-form");
  if (policy) {
    form.elements.project.readOnly = true;
    form.elements.key.readOnly = true;
  }
  const average = () => {
    $("#hf-joint-average").textContent =
      form.elements.schedule.value === "random_interval"
        ? `计划区间 ${Number(form.elements.minimum.value) / 60}–${Number(form.elements.maximum.value) / 60} 分钟，平均 ${(Number(form.elements.minimum.value) + Number(form.elements.maximum.value)) / 120} 分钟；从实际启动时间计时。`
        : `固定 ${Number(form.elements.interval.value) / 60} 分钟；从实际启动时间计时。`;
  };
  for (const n of ["minimum", "maximum", "interval", "schedule"])
    form.elements[n].oninput = average;
  average();
  if (policy)
    $("#hf-reschedule").onclick = (e) =>
      busy(e.target, async () => {
        await api(`/interactions/${policy.id}/reschedule`, "POST", {
          expected_version: policy.version,
        });
        closePanel();
        await renderInteractions(policy.id);
      });
  form.onsubmit = (e) => {
    e.preventDefault();
    busy(e.submitter, async () => {
      const config = JSON.parse(form.elements.advanced.value);
      const selected = (name) =>
        [...form.querySelectorAll(`input[name="${name}"]:checked`)].map(
          (a) => a.value,
        );
      const csv = (value) =>
        value
          .split(",")
          .map((x) => x.trim())
          .filter(Boolean);
      Object.assign(config, {
        project_id: form.elements.project.value.trim(),
        policy_key: form.elements.key.value.trim(),
        enabled: form.elements.enabled.value === "true",
        run_mode: form.elements.mode.value,
      });
      Object.assign(config.scan, {
        schedule_mode: form.elements.schedule.value,
        interval_seconds: Number(form.elements.interval.value),
        min_interval_seconds: Number(form.elements.minimum.value),
        max_interval_seconds: Number(form.elements.maximum.value),
        long_gap_reset_seconds: Number(form.elements.longgap.value),
      });
      const chosen = selected("search");
      config.search_accounts.account_ids = [
        ...new Set([
          ...form.elements.order.value
            .split("\n")
            .map((x) => x.trim())
            .filter((x) => chosen.includes(x)),
          ...chosen,
        ]),
      ];
      config.search_accounts.account_pool_id = null;
      config.search_accounts.require_different_from_previous =
        form.elements.strict.checked;
      for (const p of config.products) {
        const id = p.product_id;
        p.enabled = form.elements[`enabled-${id}`].checked;
        p.languages = csv(form.elements[`languages-${id}`].value);
        p.knowledge_version = form.elements[`knowledge-${id}`].value;
        p.target_platforms = csv(form.elements[`platforms-${id}`].value);
        p.target_versions = csv(form.elements[`versions-${id}`].value);
        p.execution_account_ids = selected(`execution-${id}`);
        p.execution_pool_id = null;
        p.preferred_execution_account_ids_by_language = {
          ...(p.preferred_execution_account_ids_by_language || {}),
        };
        for (const language of ["ko", "ja"]) {
          const preferred = form.elements[`preferred-${id}-${language}`].value;
          if (preferred)
            p.preferred_execution_account_ids_by_language[language] = [
              preferred,
            ];
          else delete p.preferred_execution_account_ids_by_language[language];
        }
        p.facts = JSON.parse(form.elements[`facts-${id}`].value);
        const previous = cfg.products.find((x) => x.product_id === id);
        const advancedGroups = JSON.parse(form.elements[`groups-${id}`].value);
        const expressions = form.elements[`queries-${id}`].value
          .split("\n")
          .map((x) => x.trim())
          .filter(Boolean);
        p.query_groups =
          JSON.stringify(expressions) ===
          JSON.stringify(
            previous.query_groups
              .filter((q) => q.enabled)
              .map((q) => q.expression),
          )
            ? advancedGroups
            : form.elements[`groups-${id}`].value ===
                JSON.stringify(previous.query_groups, null, 2)
              ? expressions.map(
                  (expression, i) =>
                    previous.query_groups.find(
                      (q) => q.expression === expression,
                    ) || {
                      id: `need-${i + 1}-${Date.now()}`,
                      expression,
                      source_type: "public_need",
                      visibility_scope: "public",
                      version: "1",
                      enabled: true,
                      max_reads_per_round: 20,
                      product_ids: [],
                    },
                )
              : advancedGroups;
      }
      const saved = await api("/interactions", "POST", {
        config,
        expected_version: policy?.version || 0,
      });
      closePanel();
      location.hash = "interactions/" + saved.id;
      await renderInteractions(saved.id);
    });
  };
}

/* Saved publication counters only. A publication cohort is not a daily increment. */
const effectKeys = ["views", "likes", "replies"];
const effectLabels = { views: "观看", likes: "点赞", replies: "回复" };
const effectStates = {
  pending: "等待采集",
  complete: "已采集",
  partial: "部分缺失",
  failed: "采集未成功",
  unavailable: "平台暂不支持",
  not_applicable: "不适用",
};
let effectsGeneration = 0;

function effectCount(value, approximate = false, delta = null) {
  if (value === null || value === undefined)
    return '<span class="effect-missing">不可获取</span>';
  return `${approximate ? "约 " : ""}${esc(value.toLocaleString("zh-CN"))}${delta === null ? "" : `<span class="sub">较上次 ${delta >= 0 ? "+" : ""}${esc(delta)}</span>`}`;
}

function effectLink(reference) {
  try {
    const url = new URL(reference);
    if (url.protocol === "https:")
      return `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">打开平台原文 ↗</a>`;
  } catch {}
  return "已保存平台回执";
}

function effectsToday() {
  return new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" });
}

function effectsDateRange(range) {
  if (!["1", "7", "30"].includes(range)) return ["", ""];
  const today = effectsToday();
  const start = new Date(today + "T00:00:00Z");
  start.setUTCDate(start.getUTCDate() - Number(range) + 1);
  return [start.toISOString().slice(0, 10), today];
}

function effectsSelection() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const requestedRange = params.get("range") || "7";
  const range = ["1", "7", "30", "all", "custom", "unknown"].includes(
    requestedRange,
  )
    ? requestedRange
    : "7";
  const dates = effectsDateRange(range);
  return {
    range,
    platform: params.get("platform") || "",
    account_id: params.get("account_id") || "",
    task_id: params.get("task_id") || "",
    q: params.get("q") || "",
    status: params.get("status") || "",
    date_from: range === "custom" ? params.get("date_from") || "" : dates[0],
    date_to: range === "custom" ? params.get("date_to") || "" : dates[1],
    publication_time: range === "unknown" ? "unknown" : "all",
    sort: params.get("sort") || "published_at",
    direction: params.get("direction") || "desc",
    limit: params.get("limit") || "50",
    offset: params.get("offset") || "0",
  };
}

function effectsQuery(values, paginate = true) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (key === "range" || (!paginate && ["limit", "offset"].includes(key)))
      continue;
    if (value !== "") query.set(key, value);
  }
  return query.toString();
}

function effectsNavigate(values) {
  $("#effects-workspace")?.setAttribute("aria-busy", "true");
  const hash = "#effects?" + new URLSearchParams(values).toString();
  if (location.hash === hash)
    renderEngagementEffects().catch((e) => toast(e.message, true));
  else location.hash = hash;
}

function effectsOptions(rows, selected, id, label, emptyLabel) {
  if (selected && !rows.some((row) => row[id] === selected))
    rows = [...rows, { [id]: selected, [label]: `未找到：${selected}` }];
  return `<option value="">${emptyLabel}</option>${rows.map((row) => `<option value="${esc(row[id])}" ${row[id] === selected ? "selected" : ""}>${esc(row[label])}</option>`).join("")}`;
}

function effectsStatus(item) {
  const state =
    item.collection_state ||
    (item.capability !== "supported"
      ? item.capability
      : item.status === "unavailable"
        ? "failed"
        : item.status);
  const tone =
    state === "complete"
      ? "green"
      : ["partial", "failed"].includes(state)
        ? "amber"
        : "";
  return `<span class="badge ${tone}">${esc(effectStates[state] || state)}</span>${item.stale ? '<span class="sub">数据待刷新</span>' : ""}`;
}

function effectsAge(timestamp) {
  const hours = Math.max(0, (Date.now() / 1000 - timestamp) / 3600);
  return hours < 1
    ? "发布不足 1 小时"
    : hours < 24
      ? `已发布 ${Math.floor(hours)} 小时`
      : `已发布 ${Math.floor(hours / 24)} 天`;
}

async function renderEngagementEffects() {
  const generation = ++effectsGeneration;
  const route = location.hash;
  const values = effectsSelection();
  const current = () =>
    generation === effectsGeneration && route === location.hash;
  $("#main").innerHTML =
    heading(
      "互动效果",
      "按每条已核验发布的内容，查看观看、点赞、回复及历史变化。",
      '<div class="actions"><a href="#statistics">发布与定时报告</a><button class="secondary" id="effects-refresh">刷新数据</button><button id="effects-export" disabled>导出筛选结果</button></div>',
    ) +
    '<section id="effects-workspace" aria-busy="true"><p class="loading">正在读取已保存的效果数据…</p></section>';
  $("#effects-refresh").onclick = () =>
    renderEngagementEffects().catch((e) => toast(e.message, true));
  let data;
  try {
    data = await api("/engagement/effects?" + effectsQuery(values));
  } catch (e) {
    if (!current()) return;
    if (
      e.status === 404 &&
      location.port !== "8782" &&
      ["localhost", "127.0.0.1"].includes(location.hostname)
    ) {
      const link = new URL(location.href);
      link.port = "8782";
      $("#effects-workspace").innerHTML =
        `<div class="surface padded"><h2>互动效果已迁入独立工作台</h2><p>当前服务仍在执行任务，新页面可从只读统计入口使用。</p><a href="${esc(link.href)}">打开互动效果工作台 →</a></div>`;
      $("#effects-workspace").setAttribute("aria-busy", "false");
      return;
    }
    $("#effects-workspace").innerHTML =
      '<div class="surface padded"><p role="alert">互动效果读取失败，请刷新重试，或重置筛选。</p><button id="effects-reset" class="secondary">重置筛选</button></div>';
    $("#effects-workspace").setAttribute("aria-busy", "false");
    $("#effects-reset").onclick = () => effectsNavigate({ range: "7" });
    throw e;
  }
  if (!current()) return;
  const select = (name, label, options) =>
    `<label>${label}<select name="${name}" id="effects-${name}">${options}</select></label>`;
  const ranges = {
    1: "今天",
    7: "最近 7 天",
    30: "最近 30 天",
    all: "全部日期",
    custom: "自定义日期",
    unknown: "发布时间未知",
  };
  const rangeOptions = Object.entries(ranges)
    .map(
      ([key, label]) =>
        `<option value="${key}" ${key === values.range ? "selected" : ""}>${label}</option>`,
    )
    .join("");
  const stateOptions = [
    { id: "stale", name: "数据待刷新" },
    ...Object.entries(effectStates).map(([id, name]) => ({ id, name })),
  ];
  const stat = (key) => {
    const total = data.totals[key];
    return `<div class="surface effect-stat"><span>累计${effectLabels[key]}</span><strong>${total.available_items ? `${total.estimated_items ? "约 " : ""}${total.observed_total.toLocaleString("zh-CN")}` : "不可获取"}</strong><small>覆盖 ${total.available_items} / ${data.total} 条${total.estimated_items ? ` · ${total.estimated_items} 条含约数` : ""}</small></div>`;
  };
  const sortHeader = (key, label) =>
    `<th scope="col" ${values.sort === key ? `aria-sort="${values.direction === "asc" ? "ascending" : "descending"}"` : ""}><button type="button" class="effect-sort" data-effect-sort="${key}">${label}<span aria-hidden="true"> ${values.sort === key ? (values.direction === "asc" ? "↑" : "↓") : "↕"}</span></button></th>`;
  const rows = data.items
    .map(
      (item) => `<tr>
    <td class="effect-content"><button type="button" class="effect-text-button" data-effect-detail="${esc(item.item_id)}"><span class="effect-excerpt">${esc(item.text || item.title || "（无文字内容）")}</span></button><div class="effect-byline">${platform(item.platform)}<span>${esc(item.account_label)}</span></div><span class="sub">任务：${esc(item.task_name)} · ${esc(actions[item.action] || item.action)}</span></td>
    <td class="effect-date">${item.published_at ? `${esc(fmt(item.published_at))}<span class="sub">${esc(effectsAge(item.published_at))}</span>` : '<span class="effect-missing">发布时间未知</span>'}</td>
    ${effectKeys.map((key) => `<td class="effect-number">${effectCount(item.metrics[key], item.estimated.includes(key), item.delta[key])}</td>`).join("")}
    <td>${effectsStatus(item)}<span class="sub">${item.observed_at ? esc(fmt(item.observed_at)) : "尚无采集记录"}</span></td>
    <td><button type="button" class="secondary compact" data-effect-detail="${esc(item.item_id)}">查看详情</button></td></tr>`,
    )
    .join("");
  const cohort =
    values.range === "unknown"
      ? "发布时间未知的互动"
      : values.date_from || values.date_to
        ? `${values.date_from || "最早"} 至 ${values.date_to || "至今"} 发布的互动`
        : "全部日期发布的互动";
  $("#effects-workspace").innerHTML = `
    <form id="effects-filter" class="surface effect-filter"><div class="effect-filter-grid">
      ${select("range", "发布日期（北京时间）", rangeOptions)}
      <label>开始日期<input type="date" name="date_from" value="${esc(values.date_from)}" max="${effectsToday()}" ${values.range !== "custom" ? "disabled" : ""}></label>
      <label>结束日期<input type="date" name="date_to" value="${esc(values.date_to)}" max="${effectsToday()}" ${values.range !== "custom" ? "disabled" : ""}></label>
      ${select(
        "platform",
        "平台",
        effectsOptions(
          data.options.platforms.map((id) => ({ id, name: names[id] || id })),
          values.platform,
          "id",
          "name",
          "全部平台",
        ),
      )}
      ${select("account_id", "账号", effectsOptions(data.options.accounts, values.account_id, "id", "label", "全部账号"))}
      ${select("task_id", "所属任务", effectsOptions(data.options.tasks, values.task_id, "id", "name", "全部任务"))}
      ${select("status", "采集状态", effectsOptions(stateOptions, values.status, "id", "name", "全部状态"))}
      <label class="effect-search">搜索内容<input name="q" type="search" maxlength="500" value="${esc(values.q)}" placeholder="正文、账号、任务或链接"></label>
      </div><div class="effect-filter-actions"><button type="submit">应用筛选</button><button id="effects-clear" type="button" class="secondary">重置</button><span class="help">按发布日期筛选，展示截至最近采集的累计值。</span></div></form>
    <div class="effect-summary" aria-label="筛选结果汇总"><div class="surface effect-stat"><span>已核验互动</span><strong>${data.total.toLocaleString("zh-CN")}</strong><small>全部筛选结果 · 非当前页</small></div>${effectKeys.map(stat).join("")}</div>
    <div class="effect-context"><span>${esc(cohort)} · 累计表现</span><span>${data.scheduler_running ? "定期采集运行中" : "定期采集未运行"} · 约每 3 小时到期读取</span></div>
    ${data.unknown_publication_total ? `<p class="help effect-unknown">有 ${data.unknown_publication_total} 条记录缺少实际发布时间，不会用计划时间代替。<button type="button" id="effects-unknown" class="effect-text-button">查看发布时间未知的互动</button></p>` : ""}
    <section class="surface" aria-labelledby="effects-list-title"><div class="section-header effect-list-header"><h2 id="effects-list-title">互动明细 <span class="muted">${data.total} 条</span></h2><div class="actions"><button type="button" class="secondary compact" data-effect-quick="views">观看最多</button><button type="button" class="secondary compact" data-effect-quick="replies">回复最多</button><button type="button" class="secondary compact" data-effect-quick="pending">等待采集</button></div></div>
      <div class="table-wrap" role="region" aria-label="互动效果明细，可横向滚动" tabindex="0"><table class="effect-table"><thead><tr><th scope="col">互动内容 / 账号</th>${sortHeader("published_at", "发布时间")}${effectKeys.map((key) => sortHeader(key, effectLabels[key])).join("")}${sortHeader("observed_at", "状态 / 最近采集")}<th scope="col">操作</th></tr></thead><tbody>${rows || '<tr><td colspan="7"><div class="empty"><h3>没有符合条件的互动</h3><p>可以调整日期、账号或采集状态；未核验的内容不计入效果。</p></div></td></tr>'}</tbody></table></div>
      <div class="effect-pagination"><span role="status">${data.total ? data.offset + 1 : 0}–${Math.min(data.offset + data.limit, data.total)} / ${data.total} 条</span><label>每页<select id="effects-limit">${[25, 50, 100].map((size) => `<option ${size === data.limit ? "selected" : ""}>${size}</option>`).join("")}</select></label><div class="actions"><button id="effects-prev" class="secondary compact" ${data.offset === 0 ? "disabled" : ""}>上一页</button><button id="effects-next" class="secondary compact" ${data.offset + data.limit >= data.total ? "disabled" : ""}>下一页</button></div></div>
    </section><details class="effect-definitions"><summary>指标口径与数据覆盖</summary><p>当前接入 X、Telegram。X 观看是帖子浏览次数，并非视频播放量或去重人数；Telegram 点赞只合计 👍 / ❤。指标属于我们发布的这一条内容，不使用原帖或相邻评论的数量。</p><p>0 表示已采到零；不可获取表示缺失。平台回复累计数与“他人直接回复”事件统计口径不同。“较上次”是相邻两次采集的差值，不是今日新增；约数不计算精确增量。数据可能因浏览器占用、账号冷却或离线而延迟，刷新页面只读取已保存数据。</p></details>`;
  $("#effects-workspace").setAttribute("aria-busy", "false");
  $("#effects-filter").onsubmit = (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const next = { ...values, ...Object.fromEntries(form), offset: "0" };
    if (
      next.range === "custom" &&
      next.date_from &&
      next.date_to &&
      next.date_from > next.date_to
    ) {
      toast("开始日期不能晚于结束日期", true);
      return;
    }
    effectsNavigate(next);
  };
  $("#effects-range").onchange = (event) => {
    const range = event.target.value,
      dates = effectsDateRange(range);
    ["date_from", "date_to"].forEach((name, index) => {
      const input = $("#effects-filter").elements[name];
      input.disabled = range !== "custom";
      if (range !== "custom") input.value = dates[index];
    });
  };
  $("#effects-clear").onclick = () => effectsNavigate({ range: "7" });
  $("#effects-unknown")?.addEventListener("click", () =>
    effectsNavigate({ ...values, range: "unknown", offset: "0" }),
  );
  document.querySelectorAll("[data-effect-sort]").forEach((button) => {
    button.onclick = () =>
      effectsNavigate({
        ...values,
        sort: button.dataset.effectSort,
        direction:
          values.sort === button.dataset.effectSort &&
          values.direction === "desc"
            ? "asc"
            : "desc",
        offset: "0",
      });
  });
  document.querySelectorAll("[data-effect-quick]").forEach((button) => {
    button.onclick = () =>
      effectsNavigate({
        ...values,
        ...(button.dataset.effectQuick === "pending"
          ? { status: "pending" }
          : { sort: button.dataset.effectQuick, direction: "desc" }),
        offset: "0",
      });
  });
  document.querySelectorAll("[data-effect-detail]").forEach((button) => {
    button.onclick = () => showEffectsDetail(button.dataset.effectDetail);
  });
  $("#effects-prev").onclick = () =>
    effectsNavigate({
      ...values,
      offset: String(Math.max(0, data.offset - data.limit)),
    });
  $("#effects-next").onclick = () =>
    effectsNavigate({ ...values, offset: String(data.offset + data.limit) });
  $("#effects-limit").onchange = (event) =>
    effectsNavigate({ ...values, limit: event.target.value, offset: "0" });
  const exportButton = $("#effects-export");
  exportButton.disabled = !data.total;
  exportButton.textContent = `导出全部 ${data.total} 条`;
  exportButton.onclick = () =>
    busy(
      exportButton,
      async () => {
        const response = await fetch(
          "/api/engagement/export?" + effectsQuery(values, false),
        );
        if (!response.ok) throw Error("导出失败，请刷新后重试");
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = url;
        link.download = `互动效果_${effectsToday()}.csv`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      },
      current,
    );
}

function effectsChart(samples, key) {
  const ordered = [...samples].reverse();
  const available = ordered.filter(
    (sample) =>
      sample.metrics[key] !== null && sample.metrics[key] !== undefined,
  );
  if (!available.length)
    return '<p class="help">暂无该指标的有效历史数据。</p>';
  const first = ordered[0].observed_at,
    last = ordered.at(-1).observed_at;
  const maximum = Math.max(
    2,
    Math.ceil(Math.max(...available.map((sample) => sample.metrics[key])) / 2) *
      2,
  );
  const x = (sample) =>
    65 +
    (last === first ? 0.5 : (sample.observed_at - first) / (last - first)) *
      425;
  const y = (sample) => 155 - (sample.metrics[key] / maximum) * 125;
  let path = "",
    connected = false;
  for (const sample of ordered) {
    if (sample.metrics[key] === null || sample.metrics[key] === undefined) {
      connected = false;
      continue;
    }
    path += `${connected ? "L" : "M"}${x(sample).toFixed(1)},${y(sample).toFixed(1)} `;
    connected = true;
  }
  return `<svg class="effect-chart" viewBox="0 0 540 205" role="img" aria-label="${effectLabels[key]}历史累计曲线，缺失采样处断开"><title>${effectLabels[key]}历史累计</title>${[0, 0.5, 1].map((ratio) => `<line class="effect-chart-grid" x1="65" x2="490" y1="${155 - ratio * 125}" y2="${155 - ratio * 125}"/><text x="55" y="${160 - ratio * 125}" text-anchor="end">${Math.round(maximum * ratio).toLocaleString("zh-CN")}</text>`).join("")}<path class="effect-chart-line" d="${path}"/>${available.map((sample) => `<circle class="effect-chart-point" cx="${x(sample)}" cy="${y(sample)}" r="3"><title>${esc(fmt(sample.observed_at))}：${sample.estimated.includes(key) ? "约 " : ""}${sample.metrics[key]}</title></circle>`).join("")}<text x="65" y="190">${esc(fmt(first))}</text>${first !== last ? `<text x="490" y="190" text-anchor="end">${esc(fmt(last))}</text>` : ""}</svg>`;
}

function showEffectsDetail(itemId) {
  const dialog = document.createElement("dialog");
  dialog.className = "effects-drawer";
  dialog.setAttribute("aria-labelledby", "effects-detail-title");
  dialog.innerHTML =
    '<div class="dialog-head"><h2 id="effects-detail-title">互动详情</h2><button type="button" class="secondary compact" id="effects-detail-close">关闭详情</button></div><div id="effects-detail-body" class="effects-detail-body" aria-live="polite">正在读取详情…</div>';
  $("#main").append(dialog);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  dialog.querySelector("#effects-detail-close").onclick = () => dialog.close();
  dialog.showModal();
  async function load(limit = 100) {
    try {
      const data = await api(
        `/engagement/items/${encodeURIComponent(itemId)}?limit=${limit}`,
      );
      if (!dialog.isConnected || !dialog.open) return;
      const item = data.item,
        body = dialog.querySelector("#effects-detail-body");
      body.innerHTML = `<div class="effect-byline">${platform(item.platform)}<strong>${esc(item.account_label)}</strong></div><p class="help">任务：${esc(item.task_name)} · ${esc(actions[item.action] || item.action)}<br>发布时间：${item.published_at ? esc(fmt(item.published_at)) : "未知"}</p><h3>我们发布的内容</h3>${item.title ? `<h3>${esc(item.title)}</h3>` : ""}<div class="effect-full-text">${esc(item.text || "（无文字内容）")}</div><p class="help">${effectLink(item.external_reference)}</p><p class="help">目标：${esc(item.target || "未记录")}</p><div class="effect-summary effect-detail-stats">${effectKeys.map((key) => `<div class="surface effect-stat"><span>累计${effectLabels[key]}</span><strong>${effectCount(item.metrics[key], item.estimated.includes(key), item.delta[key])}</strong></div>`).join("")}</div><p>${effectsStatus(item)} <span class="help">最近采集：${item.observed_at ? esc(fmt(item.observed_at)) : "尚未采集"}${item.previous_observed_at ? `<br>增量比较区间：${esc(fmt(item.previous_observed_at))} → ${esc(fmt(item.observed_at))}` : ""}</span></p><h3>历史变化</h3><div class="actions" role="group" aria-label="选择历史指标">${effectKeys.map((key) => `<button type="button" class="secondary compact" data-effect-metric="${key}" aria-pressed="${key === "views"}">${effectLabels[key]}</button>`).join("")}</div><div id="effects-detail-chart">${effectsChart(data.samples, "views")}</div><h3>采集记录</h3><p class="help">最近 ${data.samples.length} / ${data.sample_total} 次采集 · 北京时间。空缺不补零，曲线不连接缺失样本。</p><div class="table-wrap" role="region" aria-label="互动历史采集记录，可横向滚动" tabindex="0"><table class="effect-history-table"><thead><tr><th>采集时间</th>${effectKeys.map((key) => `<th>${effectLabels[key]}</th>`).join("")}<th>状态</th></tr></thead><tbody>${data.samples.map((sample) => `<tr><td>${esc(fmt(sample.observed_at))}</td>${effectKeys.map((key) => `<td>${effectCount(sample.metrics[key], sample.estimated.includes(key))}</td>`).join("")}<td>${esc(sample.status === "unavailable" ? "采集未成功" : effectStates[sample.status] || sample.status)}</td></tr>`).join("") || '<tr><td colspan="5">尚无历史采集记录</td></tr>'}</tbody></table></div>${data.sample_total > data.samples.length && limit < 1000 ? '<button type="button" id="effects-history-more" class="secondary">查看更多采集记录</button>' : ""}`;
      body.querySelectorAll("[data-effect-metric]").forEach((button) => {
        button.onclick = () => {
          body
            .querySelectorAll("[data-effect-metric]")
            .forEach((tab) =>
              tab.setAttribute("aria-pressed", String(tab === button)),
            );
          body.querySelector("#effects-detail-chart").innerHTML = effectsChart(
            data.samples,
            button.dataset.effectMetric,
          );
        };
      });
      body
        .querySelector("#effects-history-more")
        ?.addEventListener("click", (event) =>
          busy(event.target, () => load(Math.min(1000, limit * 2))),
        );
    } catch {
      if (dialog.isConnected && dialog.open)
        dialog.querySelector("#effects-detail-body").innerHTML =
          '<p role="alert">详情读取失败，请关闭后重试。</p>';
    }
  }
  load();
}

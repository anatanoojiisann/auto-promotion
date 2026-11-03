/* Publication-level counters are independent of complete reply-event coverage. */
function engagementCount(value, approximate = false, delta = null) {
  if (value === null || value === undefined) return "不可获取";
  return `${approximate ? "约 " : ""}${esc(value.toLocaleString("zh-CN"))}${delta === null ? "" : `<span class="sub">较上次 ${delta >= 0 ? "+" : ""}${esc(delta)}</span>`}`;
}

function engagementLink(reference) {
  try {
    const url = new URL(reference);
    if (url.protocol === "https:")
      return `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">查看互动 ↗</a>`;
  } catch {}
  return "已保存平台回执";
}

async function renderEngagement(offset = 0) {
  const host = $("#engagement-metrics");
  if (!host) return;
  const data = await api(`/engagement?offset=${offset}&limit=50`);
  if (!host.isConnected) return;
  const states = {
    pending: "等待采集",
    unavailable: "暂不可获取",
    not_applicable: "不适用",
    partial: "部分指标可获取",
    complete: "已采集",
  };
  const keys = ["views", "likes", "replies"];
  host.className = "surface padded mt-24";
  host.innerHTML = `<h2>观看、点赞与回复（全部日期）</h2>
    <p class="help">每 3 小时读取一次我们已核验发布的互动，保存累计数量和相邻两次的变化。X 的观看为帖子浏览次数；Telegram 的点赞仅合计 👍／❤，其他表情另计。平台回复数包含平台计入的全部回复，与上方仅统计他人直接回复的口径不同。私信不适用公开指标。页面缩写数量标“约”，缺失数据保留未知。</p>
    <p class="muted">${data.scheduler_running ? "定期采集运行中" : "定期采集未运行"} · 当前接入 X、Telegram；其他平台显示覆盖状态。电脑和服务恢复后继续读取当前数量，离线期间不会补造历史快照。</p>
    <div class="stats">${keys
      .map((key, i) => {
        const t = data.totals[key];
        return `<span class="stat"><strong>${t.available_items ? `${t.estimated_items ? "约 " : ""}${esc(t.observed_total.toLocaleString("zh-CN"))}` : "不可获取"}</strong>已观察${["观看", "点赞", "回复"][i]}<span class="sub">覆盖 ${t.available_items}／${data.total} 条，累计值</span></span>`;
      })
      .join("")}</div>
    <div class="table-wrap" role="region" aria-label="已发布互动的观看点赞回复" tabindex="0"><table><thead><tr><th>账号／互动</th><th>观看</th><th>点赞</th><th>回复</th><th>最近采集</th><th>状态／历史</th></tr></thead><tbody>${data.items.length ? data.items.map((r) => `<tr><td>${esc(r.account_label)}<span class="sub">${engagementLink(r.external_reference)}</span></td>${keys.map((k) => `<td>${engagementCount(r.metrics[k], r.estimated.includes(k), r.delta[k])}</td>`).join("")}<td>${r.observed_at ? esc(fmt(r.observed_at)) : "尚未采集"}${r.stale ? '<span class="sub">数据待刷新</span>' : ""}</td><td>${esc(states[r.status] || r.status)}${r.reason ? `<span class="sub">${esc(r.reason)}</span>` : ""}${r.observed_at ? `<button class="secondary compact engagement-history" data-item="${esc(r.item_id)}">查看历史</button>` : ""}</td></tr>`).join("") : '<tr><td colspan="6">尚无已核验发布的互动。</td></tr>'}</tbody></table></div>
    <div class="toolbar"><span class="muted">${data.total ? offset + 1 : 0}–${Math.min(offset + data.limit, data.total)}／${data.total} 条</span><button class="secondary compact" id="engagement-prev" ${offset ? "" : "disabled"}>上一页</button><button class="secondary compact" id="engagement-next" ${offset + data.limit < data.total ? "" : "disabled"}>下一页</button></div>`;
  $("#engagement-prev").onclick = () =>
    renderEngagement(Math.max(0, offset - data.limit)).catch((e) =>
      toast(e.message, true),
    );
  $("#engagement-next").onclick = () =>
    renderEngagement(offset + data.limit).catch((e) => toast(e.message, true));
  host.querySelectorAll(".engagement-history").forEach((button) => {
    button.onclick = () =>
      busy(button, async () => {
        const history = await api(
          `/items/${encodeURIComponent(button.dataset.item)}/engagement`,
        );
        const row = button.closest("tr");
        const existing = row.nextElementSibling?.classList.contains(
          "engagement-history-row",
        )
          ? row.nextElementSibling.querySelector("details")
          : null;
        if (existing) {
          existing.open = !existing.open;
          return;
        }
        const details = document.createElement("details");
        details.open = true;
        details.innerHTML = `<summary>最近 ${history.samples.length} 次采集</summary><table><thead><tr><th>采集时间</th><th>观看</th><th>点赞</th><th>回复</th></tr></thead><tbody>${history.samples.map((s) => `<tr><td>${esc(fmt(s.observed_at))}</td>${keys.map((k) => `<td>${engagementCount(s.metrics[k], s.estimated.includes(k))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
        const historyRow = document.createElement("tr");
        historyRow.className = "engagement-history-row";
        const cell = document.createElement("td");
        cell.colSpan = 6;
        cell.append(details);
        historyRow.append(cell);
        row.after(historyRow);
      });
  });
}

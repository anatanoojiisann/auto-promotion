"use strict";

async function downloadTask(taskId, format) {
  const response = await fetch(
    `/api/tasks/${encodeURIComponent(taskId)}/export?format=${format}`,
    { headers: { "X-AP-Client": "local-ui" } },
  );
  if (!response.ok) {
    const result = await response.json();
    throw Error(result.error || "导出未完成");
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = `${taskId}.${format}`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  toast(format === "zip" ? "任务日志包已下载" : "任务审阅清单已下载");
}

function bindTaskTools(task) {
  $("#refresh-task")
    .closest(".page-heading")
    .insertAdjacentHTML(
      "afterend",
      '<div class="actions task-toolbar"><button class="secondary compact" id="save-task-template">保存为模板</button><button class="secondary compact" id="download-task-review">下载审阅清单</button><button class="secondary compact" id="download-task-evidence">导出任务日志</button></div>',
    );
  $("#save-task-template").onclick = () => saveTemplateForm(task);
  $("#download-task-review").onclick = (event) =>
    busy(event.target, () => downloadTask(task.id, "html"));
  $("#download-task-evidence").onclick = (event) =>
    busy(event.target, () => downloadTask(task.id, "zip"));
}

function saveTemplateForm(task) {
  openPanel(
    "保存任务模板",
    `<form id="save-template-form"><label for="template-name">模板名称</label><input id="template-name" name="name" value="${esc(task.name)}" maxlength="160" required><label for="template-description">用途说明</label><textarea id="template-description" name="description" maxlength="2000" rows="3" placeholder="例如：AI 视频创作者的公开讨论互动"></textarea><p class="help">保存发现主题、筛选规则、间隔、顺序和限额。下次重新选择账号、日期和目标，创建新的草稿任务。</p><p class="help">原任务的审批、具体内容、已尝试结果和目标白名单不进入模板。</p><div class="form-actions"><button type="submit">保存模板</button></div></form>`,
  );
  $("#save-template-form").onsubmit = (event) => {
    event.preventDefault();
    const values = new FormData(event.target);
    busy(event.submitter, async () => {
      await api("/task-templates", "POST", {
        source_task_id: task.id,
        name: values.get("name"),
        description: values.get("description"),
      });
      $("#panel").close();
      toast("模板已保存，下次可从任务模板创建草稿");
    });
  };
}

async function renderTaskTemplates() {
  const result = await api("/task-templates");
  $("#main").innerHTML =
    heading(
      "任务模板",
      "复用发现规则和执行节奏，每次重新选择账号、日期和目标。",
      '<a class="secondary compact" href="#tasks">打开任务中心</a>',
    ) +
    `<section class="surface padded"><h2>每次任务的固定流程</h2><p>发现与核对 → 准备正文和排期 → 精确审批 → 执行与回执核验 → 统计与日志导出</p><p class="help">找群、只读采集、活动分配、调度、回执核验和统计均使用工作台现有功能。首次建立任务后，可在详情页保存常用设置。</p><div class="actions"><a href="#content">共享内容池</a><a href="#campaigns">推广活动</a><a href="#statistics">互动统计</a></div></section>` +
    (result.templates.length
      ? result.templates
          .map(
            (template) =>
              `<section class="surface padded mt-20"><div class="section-header"><h2>${esc(template.name)}</h2><button data-use-template="${esc(template.id)}">按模板创建任务</button></div><p>${esc(template.description)}</p><p class="help">${template.platforms.map((value) => esc(names[value])).join("、")} · ${template.mode === "simulation" ? "模拟任务" : "真实任务"} · 最小间隔 ${template.settings.min_interval} 秒 · ${template.settings.daily_limit} 次 / 日</p><p>发现主题：${esc(template.settings.queries.join("、") || "待核对")}</p><details><summary>查看完整设置</summary><pre>${esc(JSON.stringify(template.settings, null, 2))}</pre></details><p class="help">来源任务：<a href="#tasks/${esc(template.source_task_id)}">${esc(template.source_task_id)}</a></p></section>`,
          )
          .join("")
      : empty(
          "尚无任务模板",
          "打开已有任务，在详情页点击“保存为模板”。草稿或已结束任务的设置都可以保存。",
        ));
  document.querySelectorAll("[data-use-template]").forEach((button) => {
    button.onclick = () =>
      busy(button, async () => {
        await refresh();
        useTemplateForm(
          result.templates.find(
            (value) => value.id === button.dataset.useTemplate,
          ),
        );
      });
  });
}

function useTemplateForm(template) {
  const accounts = state.accounts.filter(
    (account) =>
      account.mode === template.mode &&
      template.platforms.includes(account.platform),
  );
  if (!accounts.length) {
    toast("请先登记模板对应平台和模式的账号", true);
    return;
  }
  const now = Date.now() + 60000;
  openPanel(
    "按模板创建任务",
    `<form id="use-template-form"><label for="template-task-name">新任务名称</label><input id="template-task-name" name="name" value="${esc(template.name)}" maxlength="160" required><fieldset><legend>本次执行账号</legend>${accounts.map((account) => `<label class="checkbox-label"><input type="checkbox" name="accounts" value="${esc(account.id)}">${esc(account.label)} · ${esc(names[account.platform])}</label>`).join("")}</fieldset><div class="field-grid"><div><label for="template-starts">执行开始时间</label><input id="template-starts" name="starts" type="datetime-local" value="${isoLocal(now)}" required></div><div><label for="template-ends">执行结束时间</label><input id="template-ends" name="ends" type="datetime-local" value="${isoLocal(now + 86400000)}" required></div></div><label for="template-targets">本次目标白名单（可选，每行一个）</label><textarea id="template-targets" name="targets" rows="3"></textarea><p class="help">目标留空时使用模板的发现政策。旧账号、目标、正文和审批均不自动带入。时间使用浏览器本地时区，保存为明确的 UTC 时间。</p><details><summary>本次复用的设置</summary><pre>${esc(JSON.stringify(template.settings, null, 2))}</pre></details><div class="form-actions"><button type="submit">创建新草稿</button></div></form>`,
  );
  $("#use-template-form").onsubmit = (event) => {
    event.preventDefault();
    const values = new FormData(event.target);
    busy(event.submitter, async () => {
      const task = await api(`/task-templates/${template.id}/tasks`, "POST", {
        name: values.get("name"),
        account_ids: values.getAll("accounts"),
        starts_at: new Date(values.get("starts")).toISOString(),
        ends_at: new Date(values.get("ends")).toISOString(),
        target_allowlist: String(values.get("targets") || "")
          .split(/\r?\n/)
          .map((value) => value.trim())
          .filter(Boolean),
      });
      $("#panel").close();
      await refresh();
      location.hash = "tasks/" + task.id;
      toast("新草稿已创建，请核对本次范围后完成审批");
    });
  };
}

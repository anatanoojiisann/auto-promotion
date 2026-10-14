/* End-to-end account-pool acceptance against an explicitly isolated simulation service. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const base = process.env.AP_POOL_UI_URL || "http://127.0.0.1:8795";
const root = path.resolve(__dirname, "..");
const output = path.join(root, "output", "playwright");
let browser;

async function screenshots(page) {
  fs.mkdirSync(output, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.evaluate(() => {
    document.activeElement?.blur();
    document.querySelector("#notice")?.classList.remove("visible");
    window.scrollTo(0, 0);
  });
  await page.screenshot({
    path: path.join(output, "pool-campaign-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  assert.ok(await page.locator("[data-nav=campaigns]").isVisible());
  assert.ok(
    await page
      .locator(".sidebar nav a")
      .evaluateAll((links) =>
        links.every((a) => a.getBoundingClientRect().height < 55),
      ),
    "Mobile navigation must stay on one readable line",
  );
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    "Only tables/navigation may scroll horizontally",
  );
  await page.screenshot({
    path: path.join(output, "pool-campaign-mobile.png"),
    fullPage: true,
  });
}

(async () => {
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.AP_UI_CHROMIUM_PATH || undefined,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1050 },
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(base);
  await page.getByRole("heading", { name: "任务中心", exact: true }).waitFor();
  const read = (route) =>
    page.evaluate((r) => fetch("/api" + r).then((x) => x.json()), route);
  const call = (route, method, body) =>
    page.evaluate(
      async ({ route, method, body }) => {
        const r = await fetch("/api" + route, {
          method,
          headers: {
            "Content-Type": "application/json",
            "X-AP-Client": "local-ui",
          },
          ...(body ? { body: JSON.stringify(body) } : {}),
        });
        const result = await r.json();
        if (!r.ok) throw Error(JSON.stringify(result));
        return result;
      },
      { route, method, body },
    );
  assert.equal(
    (await page.evaluate(() => fetch("/health").then((r) => r.json())))
      .live_writes,
    false,
  );
  assert.ok(
    (await read("/accounts")).every((a) => a.mode === "simulation"),
    "Never run UI acceptance against real accounts",
  );
  if (process.env.AP_POOL_VISUAL_ID) {
    await page.goto(`${base}/#campaigns/${process.env.AP_POOL_VISUAL_ID}`);
    await page
      .getByRole("heading", { name: "账号与人设效果", exact: true })
      .waitFor();
    await screenshots(page);
    console.log(
      JSON.stringify({ ok: true, visual_only: true, screenshots: output }),
    );
    return;
  }
  const suffix = Date.now();
  const accountIds = [];
  for (let n = 0; n < 2; n++) {
    const a = await call("/accounts", "POST", {
      platform: "telegram",
      label: `号池验收 ${suffix}-${n}`,
      backend_label: `pool-ui-${suffix}-${n}`,
      mode: "simulation",
      min_interval: 0,
      daily_limit: 20,
      hourly_limit: 20,
    });
    await call(`/accounts/${a.id}/check`, "POST");
    accountIds.push(a.id);
  }
  await page.getByRole("link", { name: "号池与人设", exact: true }).click();
  await page
    .getByRole("heading", { name: "号池与人设", exact: true })
    .waitFor();
  await page.getByRole("button", { name: "创建号池", exact: true }).click();
  const poolName = "号池界面验收 " + suffix;
  await page.getByLabel("号池名称", { exact: true }).fill(poolName);
  for (const id of accountIds)
    await page.locator(`input[name="accounts"][value="${id}"]`).check();
  await page.getByRole("button", { name: "保存号池", exact: true }).click();
  await page.getByRole("heading", { name: poolName, exact: true }).waitFor();
  for (const id of accountIds) {
    await page.locator(`[data-profile="${id}"]`).click();
    await page.getByLabel("真实业务角色", { exact: true }).fill("技术支持");
    await page.getByLabel("语言代码（每行一个）", { exact: true }).fill("en");
    await page.getByLabel("专业领域与主题", { exact: true }).fill("video");
    await page
      .getByLabel("可引用的产品事实", { exact: true })
      .fill("Approved simulation product fact.");
    await page
      .getByLabel("回复草稿模板", { exact: true })
      .fill("Regarding {title}: {facts}");
    await page.getByRole("button", { name: "保存人设", exact: true }).click();
    await page.locator("#panel").waitFor({ state: "hidden" });
  }
  await page.getByRole("link", { name: "共享内容池", exact: true }).click();
  await page
    .getByRole("button", { name: "导入 JSON / CSV", exact: true })
    .click();
  const contents = Array.from({ length: 3 }, (_, n) => ({
    platform: "telegram",
    external_id: `pool-ui-${suffix}-${n}`,
    target: `@pool_ui_${suffix}_${n}`,
    reply_to: n + 1,
    author_id: `ui-author-${suffix}-${n}`,
    title: "Video export " + n,
    context: "video export question",
    topics: ["video"],
    language: "en",
    confidence: 0.95,
  }));
  await page.locator("#content-json").fill(JSON.stringify(contents));
  await page.getByRole("button", { name: "导入并去重", exact: true }).click();
  await page.locator("#panel").waitFor({ state: "hidden" });
  await page.getByRole("button", { name: "创建活动", exact: true }).click();
  const campaignName = "活动界面验收 " + suffix;
  await page.getByLabel("活动名称", { exact: true }).fill(campaignName);
  await page
    .getByLabel("使用号池", { exact: true })
    .selectOption({ label: poolName + " · 2 个账号" });
  await page.getByLabel("账号最小间隔（秒）", { exact: true }).fill("0");
  const local = (at) =>
    new Date(at - new Date().getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
  await page
    .getByLabel("开始时间", { exact: true })
    .fill(local(Date.now() - 120000));
  await page
    .getByRole("button", { name: "创建并规划分配", exact: true })
    .click();
  await page
    .getByRole("button", { name: "创建审批批次", exact: true })
    .waitFor();
  const campaignId = await page.evaluate(() => location.hash.split("/").at(-1));
  assert.equal((await read(`/campaigns/${campaignId}`)).summary.planned, 3);
  await page.locator("[data-edit-assignment]").first().click();
  await page
    .getByLabel("正文", { exact: true })
    .fill("Reviewed exact simulation reply.");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await page.locator("#panel").waitFor({ state: "hidden" });
  await page.getByRole("button", { name: "创建审批批次", exact: true }).click();
  await page.getByRole("heading", { name: "审批批次", exact: true }).waitFor();
  const campaign = await read(`/campaigns/${campaignId}`);
  assert.equal(campaign.tasks.length, 2);
  let dueAt = 0;
  for (const task of campaign.tasks) {
    await page.goto(`${base}/#tasks/${task.id}`);
    await page.locator("#phrase-discovery_join").waitFor();
    await page
      .locator("#phrase-discovery_join")
      .fill(
        await page
          .locator('form[data-approval="discovery_join"] .phrase')
          .innerText(),
      );
    await page
      .getByRole("button", { name: "批准发现政策", exact: true })
      .click();
    await page
      .getByRole("button", { name: "冻结并预览外发清单", exact: true })
      .waitFor();
    await page
      .getByRole("button", { name: "冻结并预览外发清单", exact: true })
      .click();
    await page.locator("#phrase-outbound").waitFor();
    await page
      .locator("#phrase-outbound")
      .fill(
        await page
          .locator('form[data-approval="outbound"] .phrase')
          .innerText(),
      );
    await page
      .getByRole("button", { name: "批准外发批次", exact: true })
      .click();
    await page.getByRole("button", { name: "运行一轮", exact: true }).waitFor();
    const approved = await read(`/tasks/${task.id}`);
    assert.equal(approved.approvals.length, 2);
    dueAt = Math.max(dueAt, ...approved.items.map((i) => i.due_at * 1000));
  }
  console.log(
    "UI: 号池、人设、导入、规划、草稿编辑与两阶段审批完成，等待模拟排期到期。",
  );
  await page.waitForFunction((at) => Date.now() >= at, dueAt + 100, {
    timeout: 90000,
  });
  if ((await read("/overview")).global_stop)
    await page.locator("#global-stop").click();
  for (const task of campaign.tasks) {
    await page.goto(`${base}/#tasks/${task.id}`);
    await page.waitForFunction(
      (id) => document.querySelector("#main")?.innerText.includes(id),
      task.id,
    );
    const response = page.waitForResponse(
      (r) =>
        r.url().endsWith(`/api/tasks/${task.id}/run`) &&
        r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "运行一轮", exact: true }).click();
    assert.equal((await response).status(), 200);
    const sent = await read(`/tasks/${task.id}`);
    assert.ok(
      sent.items.length > 0 &&
        sent.items.every((i) => i.status === "confirmed"),
    );
  }
  await page.goto(`${base}/#campaigns/${campaignId}`);
  await page
    .getByRole("heading", { name: "账号与人设效果", exact: true })
    .waitFor();
  const final = await read(`/campaigns/${campaignId}`);
  assert.equal(final.execution_summary.confirmed, 3);
  assert.equal(final.state, "completed");
  assert.ok(
    final.assignments.every((a) =>
      a.external_reference.startsWith("simulation://"),
    ),
  );
  await screenshots(page);
  await page.goto(`${base}/#campaigns`);
  await page.getByRole("button", { name: "调度与额度", exact: true }).click();
  await page.getByLabel("同时执行的账号数", { exact: true }).fill("4");
  await page.getByRole("button", { name: "保存调度设置", exact: true }).click();
  await page.locator("#panel").waitFor({ state: "hidden" });
  assert.equal((await read("/execution/settings")).max_workers, 4);
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      ok: true,
      simulated_accounts: 2,
      imported_contents: 3,
      approved_batches: 2,
      confirmed_simulations: 3,
      screenshots: output,
    }),
  );
})()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    if (browser) await browser.close();
  });

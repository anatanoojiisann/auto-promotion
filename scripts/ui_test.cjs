const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, execFileSync } = require("node:child_process");
const root = path.resolve(__dirname, "..");
process.chdir(root);
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "auto-promotion-ui-"));
const python =
  process.env.AUTO_PROMOTION_PYTHON ||
  path.join(
    root,
    process.platform === "win32"
      ? ".venv/Scripts/python.exe"
      : ".venv/bin/python",
  );
const env = {
  ...process.env,
  AUTO_PROMOTION_LIVE_WRITES: "0",
  AUTO_PROMOTION_WORKSPACE: temp,
  AP_ACCOUNTS_DIR: path.join(temp, "accounts"),
  AP_CLOUD_CONTROL_URL: "",
  AP_CLOUD_NODE_TOKEN: "",
  AP_CLOUD_OPERATOR_TOKEN: "",
  AP_CLOUD_OPERATOR_TOKEN_FILE: "",
  AP_API_TOKEN: "",
};
const db = path.join(temp, "test.db");
let server, browser;
fs.mkdirSync("var", { recursive: true });
fs.mkdirSync("output/playwright", { recursive: true });
fs.rmSync("var/ui-test-result.json", { force: true });
async function startServer() {
  execFileSync(
    python,
    ["-m", "auto_promotion.cli", "--db", db, "seed-simulation"],
    { env, stdio: "pipe" },
  );
  return new Promise((resolve, reject) => {
    server = spawn(
      python,
      [
        "-m",
        "auto_promotion.cli",
        "--db",
        db,
        "serve",
        "--port",
        "0",
        "--no-worker",
      ],
      { env, stdio: ["ignore", "pipe", "pipe"] },
    );
    const timer = setTimeout(
      () => reject(new Error("Test server startup timed out")),
      20000,
    );
    server.once("error", (e) => {
      clearTimeout(timer);
      reject(e);
    });
    server.once("exit", () => {
      clearTimeout(timer);
      reject(new Error("Test server exited before readiness"));
    });
    server.stderr.on("data", (chunk) => {
      const match = chunk
        .toString()
        .match(/Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/);
      if (match) {
        clearTimeout(timer);
        resolve(match[1]);
      }
    });
  });
}
async function cleanup() {
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    const exited = new Promise((resolve) => server.once("exit", resolve));
    server.kill("SIGTERM");
    await exited;
  }
  const cleanupTarget = fs.realpathSync(temp);
  assert.equal(path.dirname(cleanupTarget), fs.realpathSync(os.tmpdir()));
  assert.ok(path.basename(cleanupTarget).startsWith("auto-promotion-ui-"));
  fs.rmSync(cleanupTarget, { recursive: true, force: true });
}

(async () => {
  const base = await startServer();
  browser = await chromium.launch({
    channel: process.env.AP_UI_BROWSER_CHANNEL || undefined,
    headless: true,
    executablePath: process.env.AP_UI_CHROMIUM_PATH || undefined,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1050 },
  });
  const errors = [];
  const expectedConsole = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") {
      if (
        m.text() ===
        "Failed to load resource: the server responded with a status of 409 (Conflict)"
      )
        expectedConsole.push(m.text());
      else errors.push(m.text());
    }
  });
  await page.goto(base);
  await page.getByRole("heading", { name: "任务中心", exact: true }).waitFor();
  const health = await page.evaluate(() =>
    fetch("/health").then((r) => r.json()),
  );
  assert.equal(
    health.live_writes,
    false,
    "UI tests require live writes disabled",
  );
  const testAccount = await page.evaluate(async () => {
    const r = await fetch("/api/accounts", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AP-Client": "local-ui",
      },
      body: JSON.stringify({
        platform: "telegram",
        label: "Telegram · 界面验收模拟 " + Date.now(),
        backend_label: "ui-simulation-" + Date.now(),
        mode: "simulation",
        min_interval: 0,
      }),
    });
    if (!r.ok) throw new Error("Could not create isolated simulation account");
    return r.json();
  });
  const secondAccount = await page.evaluate(async () => {
    const r = await fetch("/api/accounts", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AP-Client": "local-ui",
      },
      body: JSON.stringify({
        platform: "telegram",
        label: "Telegram · 第二个模拟账号",
        backend_label: "ui-simulation-second-" + Date.now(),
        mode: "simulation",
        min_interval: 0,
      }),
    });
    if (!r.ok) throw new Error("Could not create second simulation account");
    return r.json();
  });
  await page.evaluate(
    async (ids) => {
      for (const id of ids) {
        const r = await fetch(`/api/accounts/${id}/check`, {
          method: "POST",
          headers: { "X-AP-Client": "local-ui" },
        });
        if (!r.ok) throw new Error("Could not check simulation account");
      }
    },
    [testAccount.id, secondAccount.id],
  );
  await page.reload();
  await page.getByRole("heading", { name: "任务中心", exact: true }).waitFor();
  await page.getByRole("link", { name: "账号中心", exact: true }).click();
  await page.getByRole("heading", { name: "账号中心", exact: true }).waitFor();
  assert.ok((await page.locator("tbody tr").count()) >= 4);
  await page.getByRole("button", { name: "连接 IG", exact: true }).click();
  await page.getByLabel("平台用户名", { exact: true }).fill("brand");
  assert.equal(
    await page.getByLabel("Instagram 用户访问令牌").getAttribute("type"),
    "password",
  );
  await page.getByLabel("Meta 应用 API 版本").fill("v25.0");
  assert.equal(
    await page
      .locator("#connection-version")
      .evaluate((el) => el.checkValidity()),
    true,
  );
  await page.locator("#close-panel").click();
  await page.getByRole("button", { name: "连接 Discord", exact: true }).click();
  assert.equal(
    await page
      .getByLabel("Discord Bot Token", { exact: true })
      .getAttribute("type"),
    "password",
  );
  assert.equal(
    await page.getByLabel("本机账号标签", { exact: true }).inputValue(),
    "discord-bot",
  );
  await page.locator("#close-panel").click();
  await page.getByRole("button", { name: "连接 X", exact: true }).click();
  assert.ok(
    await page
      .getByRole("button", { name: "打开 X 登录", exact: true })
      .isVisible(),
  );
  assert.equal(await page.locator('input[type="password"]').count(), 0);
  await page.getByLabel("登录浏览器", { exact: true }).selectOption("msedge");
  assert.equal(
    await page.locator("#connection-browser").inputValue(),
    "msedge",
  );
  await page.locator("#close-panel").click();
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/accounts-desktop.png",
      fullPage: true,
    });
  await page.getByRole("link", { name: "API 中心", exact: true }).click();
  await page.getByRole("heading", { name: "API 中心", exact: true }).waitFor();
  assert.equal(await page.locator(".service-row").count(), 6);
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/api-desktop.png",
      fullPage: true,
    });
  await page.getByRole("link", { name: "任务中心", exact: true }).click();
  await page
    .getByRole("button", { name: "创建任务", exact: true })
    .first()
    .click();
  await page.getByLabel("任务名称").fill("界面验收 · 模拟消息 " + Date.now());
  await page
    .locator(`input[name="accounts"][value="${testAccount.id}"]`)
    .check();
  await page
    .locator(`input[name="accounts"][value="${secondAccount.id}"]`)
    .check();
  await page.getByLabel("发现主题（每行一个）").fill("AI video");
  assert.equal(await page.locator("#execution-channel option").count(), 3);
  assert.equal(
    await page.locator("#execution-channel").inputValue(),
    "inherit",
  );
  await page.locator("#task-interval").fill("0");
  await page
    .locator("#task-form")
    .getByRole("button", { name: "创建任务", exact: true })
    .click();
  await page.locator("#edit-items").waitFor();
  const originalTaskId = await page.evaluate(() =>
    location.hash.split("/").at(-1),
  );
  await page.getByRole("button", { name: "保存为模板", exact: true }).click();
  await page.getByLabel("模板名称", { exact: true }).fill("常用互动模板");
  await page.getByLabel("用途说明", { exact: true }).fill("跨任务复用设置验收");
  await page.getByRole("button", { name: "保存模板", exact: true }).click();
  await page.locator("#panel").waitFor({ state: "hidden" });
  await page.getByRole("link", { name: "任务模板", exact: true }).click();
  await page.getByRole("heading", { name: "任务模板", exact: true }).waitFor();
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/templates-desktop.png",
      fullPage: true,
    });
  await page
    .getByRole("button", { name: "按模板创建任务", exact: true })
    .click();
  await page.getByLabel("新任务名称", { exact: true }).fill("模板创建的新草稿");
  await page.locator("#template-execution-channel").selectOption("cloud");
  await page
    .locator(`input[name="accounts"][value="${testAccount.id}"]`)
    .check();
  await page.getByRole("button", { name: "创建新草稿", exact: true }).click();
  await page.locator("#edit-items").waitFor();
  const templateDraft = await page.evaluate(async () => {
    const id = location.hash.split("/").at(-1);
    return fetch(`/api/tasks/${id}`).then((response) => response.json());
  });
  assert.notEqual(templateDraft.id, originalTaskId);
  assert.deepEqual(templateDraft.approvals, []);
  assert.deepEqual(templateDraft.items, []);
  assert.equal(templateDraft.state, "draft");
  assert.equal(templateDraft.policy.execution_channel, "cloud");
  assert.equal(templateDraft.channel.requested, "cloud");
  await page
    .locator("#task-channel")
    .getByText("任务选择：在线队列", { exact: true })
    .waitFor();
  const taskRoute = `**/api/tasks/${templateDraft.id}`;
  await page.route(taskRoute, (route) =>
    route.fulfill({
      json: {
        ...templateDraft,
        channel: {
          ...templateDraft.channel,
          accounts: [
            {
              account_id: testAccount.id,
              channel: "local",
              state: "ready",
              error: null,
              retry_at: 0,
            },
          ],
        },
      },
    }),
  );
  await page.locator("#refresh-task").click();
  await page
    .locator("#task-channel")
    .getByText(/等待交接到在线队列（账号当前：本地队列）/)
    .waitFor();
  await page.unroute(taskRoute);
  await page.locator("#refresh-task").click();
  await page
    .locator("#task-channel")
    .getByText("开始任务后确定账号的实际通道。", { exact: true })
    .waitFor();
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/task-channels-desktop.png",
      fullPage: true,
    });
  const reviewDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载审阅清单", exact: true }).click();
  const review = await reviewDownload;
  await review.saveAs(path.join(temp, "task-review.html"));
  assert.ok(
    fs
      .readFileSync(path.join(temp, "task-review.html"), "utf8")
      .includes("模板创建的新草稿"),
  );
  const archiveDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出任务日志", exact: true }).click();
  const archive = await archiveDownload;
  await archive.saveAs(path.join(temp, "task-evidence.zip"));
  assert.equal(
    fs
      .readFileSync(path.join(temp, "task-evidence.zip"))
      .subarray(0, 2)
      .toString(),
    "PK",
  );
  await page.evaluate((id) => {
    location.hash = `tasks/${id}`;
  }, originalTaskId);
  await page
    .locator(".detail-top code")
    .filter({ hasText: originalTaskId })
    .waitFor();
  await page.locator("#edit-items").waitFor();
  assert.equal(await page.locator("[data-approval]").count(), 0);
  const firstTarget = "@ui_simulation_" + Date.now();
  await page.evaluate(
    async ({ accountId, target }) => {
      const taskId = location.hash.split("/").at(-1);
      const r = await fetch(`/api/tasks/${taskId}/candidates`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-AP-Client": "local-ui",
        },
        body: JSON.stringify({
          account_id: accountId,
          target,
          title: "AI video discussion",
          text: "AI video creators",
          language: "en",
          confidence: 0.95,
        }),
      });
      if (!r.ok)
        throw new Error("Could not create target assignment candidate");
    },
    { accountId: secondAccount.id, target: firstTarget },
  );
  await page.reload();
  await page.getByRole("button", { name: "编辑执行项" }).waitFor();
  await page.getByRole("button", { name: "编辑执行项" }).click();
  await page.locator('select[name="action"]').selectOption("send_message");
  await page.locator('input[name="target"]').fill(firstTarget);
  await page
    .locator('textarea[name="text"]')
    .fill("这是界面验收的模拟消息，不会发送到 Telegram。");
  const local = new Date(Date.now() - new Date().getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
  await page.locator('input[name="scheduled_at"]').fill(local);
  await page.getByRole("button", { name: "添加执行项" }).click();
  await page
    .locator('select[name="action"]')
    .nth(1)
    .selectOption("send_message");
  await page.locator('input[name="target"]').nth(1).fill("@ui_second");
  await page
    .locator('textarea[name="text"]')
    .nth(1)
    .fill("第二个模拟账号的预览消息");
  await page.getByRole("button", { name: "按已筛选目标匹配账号" }).click();
  assert.equal(
    await page.locator('select[name="account_id"]').nth(0).inputValue(),
    secondAccount.id,
  );
  await page.getByRole("button", { name: "同平台账号轮流分配" }).click();
  assert.equal(
    await page.locator('select[name="account_id"]').nth(0).inputValue(),
    testAccount.id,
  );
  assert.equal(
    await page.locator('select[name="account_id"]').nth(1).inputValue(),
    secondAccount.id,
  );
  await page.locator('[data-remove="1"]').click();
  await page.getByRole("button", { name: "保存清单", exact: true }).click();
  await page
    .locator("#task-workflow")
    .filter({ hasText: "等待开始发送" })
    .waitFor();
  await Promise.all([
    page.waitForResponse(
      (r) => r.url().endsWith("/start") && r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "开始发送", exact: true }).click(),
  ]);
  await page.getByRole("button", { name: "开始发送", exact: true }).waitFor();
  assert.equal(await page.locator("[data-approval]").count(), 0);
  const monitoredTaskId = await page.evaluate(() =>
    location.hash.split("/").at(-1),
  );
  execFileSync(
    python,
    [
      "-c",
      "import sys; from auto_promotion.store import Store; from auto_promotion.workers import Workers; w=Workers(Store(sys.argv[1])); r=w.start(sys.argv[2]); w.finish(r,'process_exit',17)",
      db,
      monitoredTaskId,
    ],
    { env, stdio: "pipe" },
  );
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await page
    .locator("#task-execution")
    .filter({ hasText: "退出码 17" })
    .waitFor();
  assert.ok(
    (await page.locator("#task-execution").innerText()).includes("退出码 17"),
  );
  const global = page.locator("#global-stop");
  if ((await global.innerText()).includes("恢复")) await global.click();
  await Promise.all([
    page.waitForResponse(
      (r) => r.url().endsWith("/start") && r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "开始发送", exact: true }).click(),
  ]);
  await page
    .locator(".item-preview .badge")
    .filter({ hasText: "已发布 · 已核验" })
    .waitFor();
  assert.ok(
    (await page.locator(".item-preview").innerText()).includes(
      "simulation://telegram/",
    ),
  );
  await page.waitForFunction(
    () => !document.querySelector("#notice").classList.contains("visible"),
  );
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/task-desktop.png",
      fullPage: true,
    });
  await global.click();
  await page.waitForFunction(
    () => !document.querySelector("#global-stop").disabled,
  );
  await page.waitForFunction(
    () => !document.querySelector("#notice").classList.contains("visible"),
  );
  const discordTask = await page.evaluate(async () => {
    const call = async (path, body) => {
      const response = await fetch("/api" + path, {
        method: body ? "POST" : "GET",
        headers: {
          "Content-Type": "application/json",
          "X-AP-Client": "local-ui",
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      if (!response.ok) throw Error("Discord test setup failed");
      return response.json();
    };
    const account = (await call("/accounts")).find(
      (a) => a.platform === "discord" && a.mode === "simulation",
    );
    const task = await call("/tasks", {
      name: "Discord · 模拟回复与图片",
      account_ids: [account.id],
      policy: {
        starts_at: new Date(Date.now() - 120000).toISOString(),
        ends_at: new Date(Date.now() + 3600000).toISOString(),
        min_interval: 0,
      },
    });
    return task;
  });
  await page.goto(base + "#tasks/" + discordTask.id);
  await page.getByRole("button", { name: "编辑执行项" }).click();
  await page.getByRole("button", { name: "读取已授权频道" }).click();
  await page
    .getByLabel("Discord 频道", { exact: true })
    .selectOption("123456789012345678");
  await page.locator('select[name="action"]').selectOption("reply");
  await page
    .getByLabel("Discord 原消息 ID（回复时）", { exact: true })
    .fill("999999999999999999");
  await page
    .locator('textarea[name="text"]')
    .fill("Discord 模拟回复 @everyone");
  await page.locator('input[name="scheduled_at"]').fill(local);
  await page.locator("input[data-upload]").setInputFiles({
    name: "discord-test.png",
    mimeType: "image/png",
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jk9sAAAAASUVORK5CYII=",
      "base64",
    ),
  });
  await page
    .getByText("discord-test.png · 单文件最多 10 MiB，合计最多 25 MiB", {
      exact: true,
    })
    .waitFor();
  assert.equal(
    await page
      .getByLabel("Discord 原消息 ID（回复时）", { exact: true })
      .inputValue(),
    "999999999999999999",
  );
  await page.getByRole("button", { name: "保存清单", exact: true }).click();
  const holdDiscord = page.locator("#global-stop");
  if (!(await holdDiscord.innerText()).includes("恢复"))
    await holdDiscord.click();
  await Promise.all([
    page.waitForResponse(
      (r) => r.url().endsWith("/start") && r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "开始发送", exact: true }).click(),
  ]);
  await page.getByRole("button", { name: "开始发送", exact: true }).waitFor();
  const savedDiscord = await page.evaluate(
    (id) => fetch(`/api/tasks/${id}`).then((r) => r.json()),
    discordTask.id,
  );
  assert.equal(
    savedDiscord.manifest.items[0].discord_reply_to,
    "999999999999999999",
  );
  assert.equal(savedDiscord.manifest.items[0].media.length, 1);
  const discordStop = page.locator("#global-stop");
  if ((await discordStop.innerText()).includes("恢复"))
    await discordStop.click();
  await Promise.all([
    page.waitForResponse(
      (r) => r.url().endsWith("/start") && r.request().method() === "POST",
    ),
    page.getByRole("button", { name: "开始发送", exact: true }).click(),
  ]);
  await page
    .locator(".item-preview .badge")
    .filter({ hasText: "已发布 · 已核验" })
    .waitFor();
  assert.ok(
    (await page.locator(".item-preview").innerText()).includes(
      "simulation://discord/",
    ),
  );
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/discord-task-desktop.png",
      fullPage: true,
    });
  await discordStop.click();
  await page.waitForFunction(
    () => !document.querySelector("#global-stop").disabled,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/task-mobile.png",
      fullPage: true,
    });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  assert.equal(overflow, false, "mobile page overflow");
  await page.getByRole("link", { name: "任务模板", exact: true }).click();
  await page.getByRole("heading", { name: "任务模板", exact: true }).waitFor();
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    ),
    false,
    "templates mobile overflow",
  );
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/templates-mobile.png",
      fullPage: true,
    });
  await page.getByRole("link", { name: "账号中心", exact: true }).click();
  await page.getByRole("heading", { name: "账号中心", exact: true }).waitFor();
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/accounts-mobile.png",
      fullPage: true,
    });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    ),
    false,
  );
  await page.getByRole("link", { name: "API 中心", exact: true }).click();
  await page.getByRole("heading", { name: "API 中心", exact: true }).waitFor();
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/api-mobile.png",
      fullPage: true,
    });
  assert.equal(
    expectedConsole.length,
    0,
    "direct start requires no approval request",
  );
  await page.evaluate(() =>
    fetch("/api/statistics/run", {
      method: "POST",
      headers: { "X-AP-Client": "local-ui" },
    }).then((r) => r.json()),
  );
  await page.getByRole("link", { name: "互动统计", exact: true }).click();
  await page.getByRole("heading", { name: "互动统计", exact: true }).waitFor();
  assert.ok((await page.locator(".stats").innerText()).includes("不可统计"));
  const statistics = await page.evaluate(() =>
    fetch("/api/statistics").then((r) => r.json()),
  );
  assert.equal(
    statistics.totals.published_success,
    0,
    "simulation must not inflate live publications",
  );
  assert.equal(statistics.totals.received_replies, null);
  assert.equal(statistics.platforms.length, 6);
  assert.equal(
    await page
      .getByRole("columnheader", { name: "当日累计发布", exact: true })
      .count(),
    1,
  );
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    ),
    false,
  );
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/statistics-mobile.png",
      fullPage: true,
    });
  await page.setViewportSize({ width: 1440, height: 1050 });
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/statistics-desktop.png",
      fullPage: true,
    });
  await page.getByRole("link", { name: "业务增长", exact: true }).click();
  await page.getByRole("heading", { name: "业务增长", exact: true }).waitFor();
  await page.getByRole("button", { name: "设置业务目标", exact: true }).click();
  await page
    .getByLabel("目标名称", { exact: true })
    .fill("Growth UI simulation");
  await page
    .getByLabel("官方 App Store 应用链接", { exact: true })
    .fill("https://apps.apple.com/app/id123456789");
  await page
    .locator('#growth-goal-form select[name="mode"]')
    .selectOption("simulation");
  const growthNow = Date.now();
  const growthStart = new Date(growthNow - 2 * 86400000);
  const growthLocal = new Date(
    growthStart.getTime() - growthStart.getTimezoneOffset() * 60000,
  )
    .toISOString()
    .slice(0, 16);
  await page.getByLabel("开始时间", { exact: true }).fill(growthLocal);
  await page.getByLabel("观察期新增下载目标", { exact: true }).fill("100");
  await page.getByLabel("观察期付费人数目标", { exact: true }).fill("10");
  await page.getByRole("button", { name: "保存目标", exact: true }).click();
  await page
    .getByRole("heading", { name: "Growth UI simulation", exact: true })
    .waitFor();
  assert.ok((await page.locator(".stats").innerText()).includes("未接入"));
  const growthId = (await page.evaluate(() => location.hash)).slice(8);
  await page.getByRole("button", { name: "添加渠道", exact: true }).click();
  await page
    .getByLabel("渠道名称", { exact: true })
    .fill("Synthetic Telegram channel");
  await page
    .getByLabel("渠道标记（英文、数字、下划线或连字符）", { exact: true })
    .fill("ui_test_tg");
  await page
    .getByLabel("提供商令牌（从 App Store Connect 获取，可稍后新建渠道）", {
      exact: true,
    })
    .fill("123456");
  await page.getByRole("button", { name: "保存渠道", exact: true }).click();
  await page.getByRole("link", { name: /pt=123456&ct=ui_test_tg/ }).waitFor();
  const growthReport = await page.evaluate(
    (id) => fetch("/api/growth/goals/" + id).then((r) => r.json()),
    growthId,
  );
  const growthChannelId = growthReport.channels[0].id;
  const conversion = (kind, hours, extra = {}) => ({
    source: "synthetic-ui",
    event_id: "ui-" + kind,
    subject_key: "ui-anonymous-user",
    channel_id: growthChannelId,
    kind,
    occurred_at: new Date(growthNow - hours * 3600000).toISOString(),
    evidence_reference: "synthetic-test",
    ...extra,
  });
  await page.getByRole("button", { name: "导入应用事件", exact: true }).click();
  await page.locator("#growth-import-json").fill(
    JSON.stringify([
      conversion("first_download", 24),
      conversion("first_generation", 23),
      conversion("trial_start", 22, {
        trial_ends_at: new Date(growthNow - 3 * 3600000).toISOString(),
      }),
      conversion("first_charge", 2, {
        transaction_id: "ui-tx",
        amount_minor: 999,
        currency: "USD",
      }),
      conversion("refund", 1, {
        transaction_id: "ui-tx",
        amount_minor: 200,
        currency: "USD",
      }),
    ]),
  );
  await page.getByRole("button", { name: "核对并导入", exact: true }).click();
  await page
    .getByText("USD：扣费 999 · 退款 200 · 净收入 799（最小货币单位）", {
      exact: true,
    })
    .waitFor();
  await page
    .getByRole("button", { name: "导入商店指标与成本", exact: true })
    .click();
  await page.locator("#growth-import-json").fill(
    JSON.stringify([
      {
        channel_id: growthChannelId,
        day: new Date(growthNow - 86400000).toLocaleDateString("sv-SE", {
          timeZone: "Asia/Shanghai",
        }),
        source: "synthetic-ui",
        evidence_reference: "synthetic-test",
        first_downloads: 12,
        spend_minor: 0,
        currency: "USD",
      },
    ]),
  );
  await page.getByRole("button", { name: "核对并导入", exact: true }).click();
  await page.getByText("目标完成 12.0%", { exact: true }).waitFor();
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/growth-desktop.png",
      fullPage: true,
    });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    ),
    false,
    "growth mobile overflow",
  );
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/growth-mobile.png",
      fullPage: true,
    });
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.getByRole("link", { name: "回复待办", exact: true }).click();
  await page.getByRole("heading", { name: "回复待办", exact: true }).waitFor();
  await page.evaluate(() => {
    location.hash = "feeds";
  });
  await page
    .getByRole("heading", { name: "内容采集来源", exact: true })
    .waitFor();
  await page.getByRole("button", { name: "添加来源", exact: true }).click();
  await page.getByLabel("公开群", { exact: true }).fill("@ui_test_group");
  await page.getByLabel("启用定时只读采集", { exact: true }).check();
  await page.getByRole("button", { name: "保存来源", exact: true }).click();
  await page
    .locator("td")
    .filter({ hasText: "@ui_test_group" })
    .first()
    .waitFor();
  await page.getByRole("button", { name: "运行到期采集", exact: true }).click();
  await page.locator("td").filter({ hasText: "读取完成" }).first().waitFor();
  await page
    .getByRole("link", { name: "共享内容池", exact: true })
    .first()
    .click();
  await page
    .getByRole("heading", { name: "共享内容池", exact: true })
    .waitFor();
  await page
    .getByRole("button", { name: "核对资格与文案", exact: true })
    .first()
    .click();
  const reviewPayload = JSON.parse(
    await page.locator("#content-review-json").inputValue(),
  );
  reviewPayload.permission = "allowed";
  reviewPayload.permission_evidence = "Synthetic rule for simulation";
  reviewPayload.draft_text = "Reviewed simulation draft";
  await page
    .locator("#content-review-json")
    .fill(JSON.stringify(reviewPayload));
  await page.getByRole("button", { name: "保存核对结果", exact: true }).click();
  await page.getByText("许可已登记", { exact: true }).waitFor();
  assert.equal(
    (
      await page.evaluate(() =>
        fetch("/api/growth/goals").then((r) => r.json()),
      )
    ).every((g) => g.payload.mode === "simulation"),
    true,
  );
  const xAccount = await page.evaluate(async () => {
    const accounts = await fetch("/api/accounts").then((r) => r.json());
    if (accounts.some((a) => a.mode !== "simulation"))
      throw new Error("Original-post UI tests require synthetic accounts");
    return accounts.find((a) => a.platform === "x");
  });
  await page.getByRole("link", { name: "任务中心", exact: true }).click();
  await page.getByRole("button", { name: "创建任务", exact: true }).click();
  await page.locator("#task-name").fill("X original UI simulation");
  await page
    .locator(`#task-form [name=accounts][value="${xAccount.id}"]`)
    .check();
  await page.locator("#task-form [name=allow_owned_x_posts]").check();
  assert.equal(await page.locator("#execution-channel").inputValue(), "cloud");
  assert.equal(
    await page
      .locator("#task-form [name=outbound_after_tasks_minutes]")
      .inputValue(),
    "60",
  );
  await page.locator("#task-form button[type=submit]").click();
  await page.locator("#edit-items").click();
  assert.equal(
    await page.locator("#items-form [name=action]").inputValue(),
    "post",
  );
  assert.equal(
    await page.locator("#items-form [name=target]").inputValue(),
    xAccount.owned_target_url,
  );
  assert.equal(
    await page.locator("#items-form [name=target]").getAttribute("readonly"),
    "",
  );
  assert.equal(
    await page.locator("#items-form [name=target_owned]").isChecked(),
    true,
  );
  assert.equal(
    await page.locator("#items-form [name=title]").getAttribute("type"),
    "hidden",
  );
  assert.equal(
    await page.locator("#items-form [name=reply_to]").isVisible(),
    false,
  );
  assert.equal(
    await page.locator('#items-form input[type="file"]').getAttribute("accept"),
    "image/png,image/jpeg",
  );
  await page
    .locator("#items-form [name=text]")
    .fill("Synthetic owned original post; no publication is started.");
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/x-original-post-editor.png",
      fullPage: true,
    });
  await page.getByRole("button", { name: "保存清单", exact: true }).click();
  const originalDraft = await page.evaluate(async () => {
    const tasks = await fetch("/api/tasks").then((r) => r.json());
    const task = tasks.find((t) => t.name === "X original UI simulation");
    return fetch("/api/tasks/" + task.id).then((r) => r.json());
  });
  assert.equal(originalDraft.state, "draft");
  assert.equal(originalDraft.policy.allow_owned_x_posts, true);
  assert.equal(originalDraft.items[0].payload.action, "post");
  assert.equal(originalDraft.items[0].payload.target_owned, true);
  assert.equal(originalDraft.items[0].attempt_id, null);
  // Synthetic display evidence only; never change an account or a real cooldown.
  let healthDisplay = "cooldown";
  const healthRoute = async (route) => {
    const response = await route.fetch();
    const accounts = await response.json();
    const account = accounts.find((a) => a.id === testAccount.id);
    account.connection_state = healthDisplay;
    account.status = healthDisplay === "cooldown" ? "healthy" : "unavailable";
    account.cooldown_until =
      healthDisplay === "cooldown" ? Date.now() / 1000 + 3600 : 0;
    account.health = {
      last_verified_at: Date.now() / 1000 - 60,
      reason: "BrowserNavigationAccessDenied",
      execution_step: "home_navigation",
    };
    await route.fulfill({ response, json: accounts });
  };
  await page.route("**/api/accounts", healthRoute);
  await page.goto(base + "/#accounts");
  // A hash-only navigation renders cached state. Reload to consume this fixture.
  await page.reload();
  await page.getByRole("heading", { name: "账号中心", exact: true }).waitFor();
  let healthRow = page
    .locator("tr")
    .filter({ has: page.locator(`[data-check="${testAccount.id}"]`) });
  await healthRow.getByText("冷却中", { exact: true }).waitFor();
  assert.equal(await healthRow.locator(".badge.green").count(), 0);
  healthDisplay = "navigation_blocked";
  await page.reload();
  await page.getByRole("heading", { name: "账号中心", exact: true }).waitFor();
  healthRow = page
    .locator("tr")
    .filter({ has: page.locator(`[data-check="${testAccount.id}"]`) });
  await healthRow.getByText("浏览器导航受阻", { exact: true }).waitFor();
  assert.ok((await healthRow.innerText()).includes("不代表已退出登录"));
  assert.ok((await healthRow.innerText()).includes("发送前仍重新核验"));
  await page.unroute("**/api/accounts", healthRoute);
  fs.writeFileSync(
    "var/ui-test-result.json",
    JSON.stringify(
      {
        passed: errors.length === 0,
        errors,
        unexpected_conflict_responses: expectedConsole.length,
        checks: [
          "three_centers",
          "cooldown_overrides_old_healthy",
          "navigation_failure_is_not_logout",
          "instagram_connection_form",
          "x_connection_form",
          "discord_connection_form",
          "discord_channel_selection",
          "discord_reply_id_precision",
          "discord_attachment",
          "discord_simulation_execution",
          "create_task",
          "task_execution_channel_selector",
          "template_channel_override",
          "task_actual_channel_display",
          "task_waiting_for_channel_handoff",
          "save_task_template",
          "template_creates_fresh_unapproved_draft",
          "review_download",
          "evidence_zip_download",
          "templates_mobile_no_overflow",
          "no_approval_forms",
          "draft_preparation_without_approval",
          "edit_items",
          "freeze_manifest",
          "execution_readiness_reasons",
          "direct_start",
          "execute_simulation",
          "stopped_sender_not_active",
          "observed_exit_code",
          "verified_reference",
          "global_stop_restore",
          "mobile_no_overflow",
          "live_statistics_exclude_simulation",
          "unavailable_replies_not_zero",
          "growth_goals_and_channel_link",
          "event_cohort_maturity_and_refund",
          "store_metrics_separate_from_events",
          "growth_mobile_no_overflow",
          "reply_inbox_entry",
          "read_only_content_feed",
          "content_review",
          "owned_x_post_task_opt_in",
          "owned_x_post_readonly_target",
          "owned_x_post_draft_does_not_publish",
          "owned_x_post_no_unrelated_fields",
        ],
      },
      null,
      2,
    ),
  );
  assert.deepEqual(errors, [], "browser errors");
  console.log(
    "UI workflow passed; 45 checks; screenshots " +
      (process.env.AP_UI_SKIP_SCREENSHOTS ? "skipped" : "saved") +
      ".",
  );
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(cleanup);

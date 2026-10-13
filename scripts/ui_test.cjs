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
  fs.rmSync(temp, { recursive: true, force: true });
}

(async () => {
  const base = await startServer();
  browser = await chromium.launch({
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
  assert.equal(await page.locator(".service-row").count(), 5);
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
  await page.locator("#task-interval").fill("0");
  await page.getByRole("button", { name: "创建并预览发现政策" }).click();
  await page.locator("#phrase-discovery_join").waitFor();
  const discovery = await page
    .locator('form[data-approval="discovery_join"] .phrase')
    .innerText();
  await page.locator("#phrase-discovery_join").fill("好的");
  await page.getByRole("button", { name: "批准发现政策" }).click();
  await page.getByRole("status").filter({ hasText: "完整确认短语" }).waitFor();
  await page.locator("#phrase-discovery_join").fill(discovery);
  await page.getByRole("button", { name: "批准发现政策" }).click();
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
  await page.getByRole("button", { name: "冻结并预览外发清单" }).click();
  await page.locator("#phrase-outbound").waitFor();
  const outbound = await page
    .locator('form[data-approval="outbound"] .phrase')
    .innerText();
  await page.locator("#phrase-outbound").fill(outbound);
  await page.getByRole("button", { name: "批准外发批次" }).click();
  await page.getByRole("button", { name: "运行一轮" }).waitFor();
  const global = page.locator("#global-stop");
  if ((await global.innerText()).includes("恢复")) await global.click();
  await page.getByRole("button", { name: "运行一轮" }).click();
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
    await call(`/tasks/${task.id}/approve/discovery_join`, {
      phrase: task.discovery_phrase,
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
  await page.getByRole("button", { name: "冻结并预览外发清单" }).click();
  const discordOutbound = await page
    .locator('form[data-approval="outbound"] .phrase')
    .innerText();
  await page.locator("#phrase-outbound").fill(discordOutbound);
  await page.getByRole("button", { name: "批准外发批次" }).click();
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
  await page.getByRole("button", { name: "运行一轮" }).click();
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
    1,
    "exactly one intentional invalid approval response",
  );
  fs.writeFileSync(
    "var/ui-test-result.json",
    JSON.stringify(
      {
        passed: errors.length === 0,
        errors,
        expected_invalid_approval_responses: expectedConsole.length,
        checks: [
          "three_centers",
          "instagram_connection_form",
          "x_connection_form",
          "discord_connection_form",
          "discord_channel_selection",
          "discord_reply_id_precision",
          "discord_attachment",
          "discord_simulation_execution",
          "create_task",
          "reject_generic_approval",
          "discovery_approval",
          "edit_items",
          "freeze_manifest",
          "outbound_approval",
          "execute_simulation",
          "verified_reference",
          "global_stop_restore",
          "mobile_no_overflow",
        ],
      },
      null,
      2,
    ),
  );
  assert.deepEqual(errors, [], "browser errors");
  console.log(
    "UI workflow passed; 18 checks; screenshots " +
      (process.env.AP_UI_SKIP_SCREENSHOTS ? "skipped" : "saved") +
      ".",
  );
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(cleanup);

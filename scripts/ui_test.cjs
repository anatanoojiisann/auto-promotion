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
  process.env.AUTO_PROMOTION_PYTHON || path.join(root, ".venv/bin/python");
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
  browser = await chromium.launch({ headless: true });
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
  await page.reload();
  await page.getByRole("heading", { name: "任务中心", exact: true }).waitFor();
  await page.getByRole("link", { name: "账号中心", exact: true }).click();
  await page.getByRole("heading", { name: "账号中心", exact: true }).waitFor();
  assert.ok((await page.locator("tbody tr").count()) >= 4);
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: "output/playwright/accounts-desktop.png",
      fullPage: true,
    });
  await page.getByRole("link", { name: "API 中心", exact: true }).click();
  await page.getByRole("heading", { name: "API 中心", exact: true }).waitFor();
  assert.equal(await page.locator(".service-row").count(), 4);
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
  await page.getByRole("button", { name: "编辑执行项" }).waitFor();
  await page.getByRole("button", { name: "编辑执行项" }).click();
  await page.locator('select[name="action"]').selectOption("send_message");
  await page
    .locator('input[name="target"]')
    .fill("@ui_simulation_" + Date.now());
  await page
    .locator('textarea[name="text"]')
    .fill("这是界面验收的模拟消息，不会发送到 Telegram。");
  const local = new Date(Date.now() - new Date().getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
  await page.locator('input[name="scheduled_at"]').fill(local);
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
    "UI workflow passed; 11 checks; screenshots " +
      (process.env.AP_UI_SKIP_SCREENSHOTS ? "skipped" : "saved") +
      ".",
  );
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(cleanup);

/* End-to-end effects checks against a temporary store, with all workers disabled. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, execFileSync } = require("node:child_process");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "..");
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "ap-effects-ui-"));
const output = path.join(root, "output/playwright/engagement-effects");
const python =
  process.env.AUTO_PROMOTION_PYTHON ||
  path.join(
    root,
    process.platform === "win32"
      ? ".venv/Scripts/python.exe"
      : ".venv/bin/python",
  );
const fixture = path.join(__dirname, "engagement_ui_fixture.py");
const db = path.join(temp, "effects.db");
const env = {
  PATH: process.env.PATH,
  PYTHONPATH: path.join(root, "src"),
  PYTHONIOENCODING: "utf-8",
  AUTO_PROMOTION_WORKSPACE: temp,
  AP_DB: db,
  AP_ACCOUNTS_DIR: path.join(temp, "accounts"),
  AUTO_PROMOTION_LIVE_WRITES: "0",
  AP_API_TOKEN: "fixture",
};
for (const key of [
  "SYSTEMROOT",
  "WINDIR",
  "TEMP",
  "TMP",
  "USERPROFILE",
  "LOCALAPPDATA",
])
  if (process.env[key]) env[key] = process.env[key];
let server, browser;
let serverLogs = "";
async function start() {
  const expected = JSON.parse(
    execFileSync(python, [fixture, db], { cwd: root, env, encoding: "utf8" }),
  );
  const base = await new Promise((resolve, reject) => {
    server = spawn(python, [fixture, db, "--serve"], {
      cwd: root,
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let logs = "";
    const timeout = setTimeout(
      () => reject(Error("Fixture startup timed out: " + logs)),
      20000,
    );
    server.once("error", reject);
    server.once("exit", () => {
      clearTimeout(timeout);
      reject(Error("Fixture exited: " + logs));
    });
    server.stderr.on("data", (chunk) => {
      serverLogs = (serverLogs + chunk).slice(-12000);
      logs = (logs + chunk).slice(-4000);
      const match = logs.match(
        /Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/,
      );
      if (match) {
        clearTimeout(timeout);
        resolve(match[1]);
      }
    });
  });
  return { base, expected };
}
async function cleanup() {
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    const exited = new Promise((resolve) => server.once("exit", resolve));
    server.kill();
    await exited;
  }
  const resolved = fs.realpathSync(temp);
  assert.equal(path.dirname(resolved), fs.realpathSync(os.tmpdir()));
  assert.ok(path.basename(resolved).startsWith("ap-effects-ui-"));
  fs.rmSync(resolved, { recursive: true, force: true });
}
(async () => {
  const { base, expected } = await start();
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.AP_UI_CHROMIUM_PATH || undefined,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400)
      errors.push(`${response.status()} ${response.url()}`);
  });
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.route("**/*", (route) =>
    new URL(route.request().url()).origin === base
      ? route.continue()
      : route.abort(),
  );
  const ready = () =>
    page.locator("#effects-workspace[aria-busy=false]").waitFor();
  const apply = async () => {
    await page.getByRole("button", { name: "应用筛选", exact: true }).click();
    await ready();
  };
  const navigate = async (query = "") => {
    await page.goto(base + "/#effects" + query);
    await ready();
  };
  await navigate();
  assert.equal(
    await page
      .locator(".effect-summary > .effect-stat")
      .first()
      .locator("strong")
      .innerText(),
    String(expected.recent_total),
  );
  assert.equal(await page.locator(".effect-table tbody tr").count(), 50);
  assert.equal(
    await page.locator("[data-nav=effects]").getAttribute("aria-current"),
    "page",
  );
  await page.locator("#effects-range").selectOption("all");
  await apply();
  assert.equal(
    await page
      .locator(".effect-summary > .effect-stat")
      .first()
      .locator("strong")
      .innerText(),
    String(expected.total),
  );
  const summary = await page.locator(".effect-summary").innerText();
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await ready();
  assert.equal(await page.locator(".effect-table tbody tr").count(), 10);
  assert.equal(await page.locator(".effect-summary").innerText(), summary);
  const downloadPromise = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "导出全部 60 条", exact: true })
    .click();
  const download = await downloadPromise;
  const csv = fs.readFileSync(await download.path(), "utf8");
  assert.ok(csv.startsWith("\ufeff"));
  assert.equal(
    (csv.match(/fx-item-/g) || []).length,
    60,
    "export must include all pages",
  );
  assert.ok(
    csv.includes("' =SUM(1,1)"),
    "formula-like content must remain inert",
  );
  await page.getByRole("button", { name: "观看最多", exact: true }).click();
  await ready();
  assert.equal(
    await page
      .locator(".effect-table tbody tr")
      .first()
      .locator(".effect-number")
      .first()
      .innerText(),
    "570\n较上次 -1",
  );
  await page.locator("[data-effect-sort=views]").click();
  await ready();
  assert.ok(
    (
      await page
        .locator(".effect-table tbody tr")
        .first()
        .locator(".effect-content")
        .innerText()
    ).includes("=SUM"),
  );
  assert.equal(await page.evaluate(() => window.effectsInjected), undefined);
  const beforeDetail = page.url();
  await page
    .locator(".effect-table tbody tr")
    .first()
    .getByRole("button", { name: "查看详情", exact: true })
    .click();
  await page.getByRole("heading", { name: "采集记录", exact: true }).waitFor();
  assert.ok(
    (await page.locator("#effects-detail-chart svg").boundingBox()).height >
      100,
  );
  assert.equal(
    await page.locator(".effect-history-table tbody tr").count(),
    100,
  );
  assert.equal(await page.locator(".effect-full-text img").count(), 0);
  await page.locator("[data-effect-metric=replies]").click();
  assert.equal(
    await page
      .locator("[data-effect-metric=replies]")
      .getAttribute("aria-pressed"),
    "true",
  );
  assert.ok(
    (
      await page.locator("#effects-detail-chart svg").getAttribute("aria-label")
    ).startsWith("回复"),
  );
  await page
    .getByRole("button", { name: "查看更多采集记录", exact: true })
    .click();
  await page.waitForFunction(
    () =>
      document.querySelectorAll(".effect-history-table tbody tr").length ===
      105,
  );
  fs.mkdirSync(output, { recursive: true });
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({ path: path.join(output, "detail-desktop.png") });
  await page.getByRole("button", { name: "关闭详情", exact: true }).click();
  assert.equal(page.url(), beforeDetail);
  await page.reload();
  await ready();
  assert.equal(await page.locator("#effects-range").inputValue(), "all");
  assert.equal(
    await page
      .locator("[data-effect-sort=views]")
      .locator("..")
      .getAttribute("aria-sort"),
    "ascending",
  );
  await page.locator("#effects-platform").selectOption("instagram");
  await apply();
  assert.equal(await page.locator(".effect-table tbody tr").count(), 1);
  assert.ok(
    (await page.locator(".effect-table tbody").innerText()).includes(
      "平台暂不支持",
    ),
  );
  await page.locator("#effects-platform").selectOption("");
  await page.locator("#effects-status").selectOption("pending");
  await apply();
  assert.equal(await page.locator(".effect-table tbody tr").count(), 1);
  assert.ok(
    (await page.locator(".effect-table tbody").innerText()).includes(
      "等待采集",
    ),
  );
  await page.locator("#effects-status").selectOption("");
  await page.locator("#effects-account_id").selectOption("fx-tg");
  await page.locator("#effects-task_id").selectOption("fx-task-b");
  await apply();
  assert.equal(
    await page
      .locator(".effect-summary > .effect-stat")
      .first()
      .locator("strong")
      .innerText(),
    "1",
  );
  await page.locator("#effects-account_id").selectOption("");
  await page.locator("#effects-task_id").selectOption("");
  await page
    .getByRole("searchbox", { name: "搜索内容" })
    .fill("does-not-exist");
  await apply();
  await page.getByRole("heading", { name: "没有符合条件的互动" }).waitFor();
  assert.equal(await page.locator("#effects-export").isDisabled(), true);
  await navigate("?range=all");
  await page.locator("#effects-unknown").click();
  await ready();
  assert.equal(await page.locator("#effects-range").inputValue(), "unknown");
  assert.equal(
    await page
      .locator(".effect-summary > .effect-stat")
      .first()
      .locator("strong")
      .innerText(),
    "1",
  );
  await navigate("?range=all&sort=views&direction=desc");
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({ path: path.join(output, "effects-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    ),
    false,
  );
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({
      path: path.join(output, "effects-mobile.png"),
      fullPage: true,
    });
  await page
    .locator(".effect-table tbody tr")
    .first()
    .getByRole("button", { name: "查看详情", exact: true })
    .click();
  await page.getByRole("heading", { name: "采集记录", exact: true }).waitFor();
  const rect = await page.locator("dialog.effects-drawer").boundingBox();
  assert.ok(rect.width <= 390 && rect.x >= 0);
  await page.locator("[data-effect-metric=replies]").click();
  assert.deepEqual(
    (await page.locator("#effects-detail-chart text").allTextContents()).slice(
      0,
      3,
    ),
    ["0", "1", "2"],
  );
  assert.ok(
    (await page.locator("#effects-detail-chart svg").boundingBox()).height >
      100,
  );
  if (!process.env.AP_UI_SKIP_SCREENSHOTS)
    await page.screenshot({ path: path.join(output, "detail-mobile.png") });
  await page.keyboard.press("Escape");
  await page.goto(base + "/#statistics");
  await page.getByRole("heading", { name: "互动统计", exact: true }).waitFor();
  await page.getByRole("link", { name: "打开互动效果 →", exact: true }).click();
  await ready();
  assert.deepEqual(errors, []);
  const proof = JSON.parse(
    execFileSync(
      python,
      [
        "-c",
        "import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); print(json.dumps({'attempts':c.execute('select count(*) from attempts').fetchone()[0],'jobs':c.execute('select count(*) from execution_jobs').fetchone()[0],'stop':c.execute(\"select value from meta where key='global_stop'\").fetchone()[0]}))",
        db,
      ],
      { env, encoding: "utf8" },
    ),
  );
  assert.deepEqual(proof, { attempts: 0, jobs: 0, stop: "1" });
  await page.route("**/api/engagement/effects?**", (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: '{"detail":"Not Found"}',
    }),
  );
  await navigate("?range=all");
  const handoff = page.getByRole("link", {
    name: "打开互动效果工作台 →",
    exact: true,
  });
  assert.equal(new URL(await handoff.getAttribute("href")).port, "8782");
  assert.ok(errors.every((message) => message.includes("404")));
  const result = {
    passed: true,
    checks: [
      "date_cohort",
      "filters",
      "global_summary",
      "pagination",
      "numeric_sort",
      "missing_values",
      "detail_history",
      "history_more",
      "metric_switch",
      "persist_filters",
      "export_all_pages",
      "formula_escape",
      "html_escape",
      "empty_state",
      "unknown_publication",
      "desktop_mobile",
      "legacy_reports",
      "old_sender_handoff",
      "zero_outbound",
    ],
  };
  fs.writeFileSync(
    path.join(output, "result.json"),
    JSON.stringify(result, null, 2),
  );
  console.log(JSON.stringify(result));
})()
  .catch((error) => {
    console.error(error);
    console.error(serverLogs);
    process.exitCode = 1;
  })
  .finally(cleanup);

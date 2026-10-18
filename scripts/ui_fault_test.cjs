/* Deterministic old-response injections against a temporary simulation service. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, execFileSync } = require("node:child_process");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "..");
const temp = fs.realpathSync(
  fs.mkdtempSync(path.join(os.tmpdir(), "ap-ui-faults-")),
);
const python =
  process.env.AUTO_PROMOTION_PYTHON ||
  path.join(
    root,
    process.platform === "win32"
      ? ".venv/Scripts/python.exe"
      : ".venv/bin/python",
  );
const env = {
  PATH: process.env.PATH,
  HOME: temp,
  TMPDIR: fs.realpathSync(os.tmpdir()),
  AP_ACCOUNTS_DIR: path.join(temp, "accounts"),
  AUTO_PROMOTION_WORKSPACE: temp,
  AUTO_PROMOTION_LIVE_WRITES: "0",
  PYTHONPATH: path.join(root, "src"),
};
let server, browser, timer;
const checks = [];
function gate() {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
}
async function pageFor(base, viewport) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  page.setDefaultTimeout(6000);
  await page.goto(base + "/#campaigns");
  const health = await page.evaluate(() =>
    fetch("/health").then((r) => r.json()),
  );
  assert.equal(health.live_writes, false);
  assert.deepEqual(
    await page.evaluate(() => fetch("/api/accounts").then((r) => r.json())),
    [],
  );
  return { context, page };
}
async function slowGet(
  base,
  viewport,
  iteration,
  failure = false,
  routeOnly = false,
) {
  const { context, page } = await pageFor(base, viewport);
  const captured = gate(),
    release = gate();
  let intercepted = false;
  try {
    await page.route("**/api/execution/settings", async (route) => {
      if (route.request().method() === "GET" && !intercepted) {
        intercepted = true;
        captured.resolve();
        await release.promise;
        if (failure) {
          await route.fulfill({
            status: 500,
            json: { error: "synthetic-old-get-failure" },
          });
          return;
        }
      }
      await route.continue();
    });
    await page.locator("#execution-settings").evaluate((button) => {
      window.oldSettingsButton = button;
      button.click();
    });
    await captured.promise;
    await page.getByRole("link", { name: "号池与人设", exact: true }).click();
    await page.getByRole("button", { name: "创建号池", exact: true }).waitFor();
    if (routeOnly) {
      release.resolve();
      await page.waitForFunction(() => !window.oldSettingsButton.disabled);
      assert.equal(
        await page.locator("#panel").evaluate((el) => el.open),
        false,
      );
      assert.equal(await page.getByRole("alert").count(), 0);
      assert.equal(await page.locator("#notice").textContent(), "");
      checks.push({
        scenario: "slow-get-route-only",
        failure,
        width: viewport.width,
        iteration,
      });
      return;
    }
    await page.getByRole("button", { name: "创建号池", exact: true }).click();
    const draft = "new-pool-draft-" + iteration;
    const input = page.getByLabel("号池名称", { exact: true });
    await input.fill(draft);
    release.resolve();
    await page.waitForFunction(() => !window.oldSettingsButton.disabled);
    assert.equal(await page.locator("#panel").evaluate((el) => el.open), true);
    assert.equal(
      await input.count(),
      1,
      "old settings GET replaced the newer pool draft",
    );
    assert.equal(await input.inputValue(), draft);
    assert.equal(await page.getByRole("alert").count(), 0);
    assert.equal(await page.locator("#notice").textContent(), "");
    assert.equal(
      await input.evaluate((el) => document.activeElement === el),
      true,
    );
    checks.push({
      scenario: "slow-get-keeps-new-draft",
      failure,
      width: viewport.width,
      iteration,
    });
  } finally {
    release.resolve();
    await context.close();
  }
}
async function slowPut(
  base,
  viewport,
  iteration,
  failure = false,
  close = "button",
) {
  const { context, page } = await pageFor(base, viewport);
  const captured = gate(),
    release = gate();
  let intercepted = false;
  try {
    await page.route("**/api/execution/settings", async (route) => {
      if (route.request().method() === "PUT" && !intercepted) {
        intercepted = true;
        // Apply A at the test server, then delay only its RESPONSE. This is not
        // a cross-client or reversed server-write ordering test.
        const response = await route.fetch();
        captured.resolve();
        await release.promise;
        if (failure)
          await route.fulfill({
            status: 500,
            json: { error: "synthetic-old-put-failure" },
          });
        else await route.fulfill({ response });
        return;
      }
      await route.continue();
    });
    await page.getByRole("button", { name: "调度与额度", exact: true }).click();
    await page.getByLabel("X · Tabbit 并发", { exact: true }).fill("2");
    await page.evaluate(
      () =>
        (window.oldSaveButton = document.querySelector(
          "#execution-form button",
        )),
    );
    await page
      .getByRole("button", { name: "保存调度设置", exact: true })
      .click();
    await captured.promise;
    if (close === "synchronous") {
      await page.evaluate(() => {
        closePanel();
        executionSettingsForm();
      });
    } else {
      if (close === "escape") await page.keyboard.press("Escape");
      else await page.locator("#close-panel").click();
      await page
        .getByRole("button", { name: "调度与额度", exact: true })
        .click();
    }
    const input = page.getByLabel("X · Tabbit 并发", { exact: true });
    await input.fill("3");
    release.resolve();
    await page.waitForFunction(() => !window.oldSaveButton.disabled);
    assert.equal(
      await page.locator("#panel").evaluate((el) => el.open),
      true,
      "old PUT response closed a newer edit",
    );
    assert.equal(await input.inputValue(), "3");
    assert.equal(await page.getByRole("alert").count(), 0);
    assert.equal(await page.locator("#notice").textContent(), "");
    assert.equal(
      await input.evaluate((el) => document.activeElement === el),
      true,
    );
    const [saved] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.url().endsWith("/api/execution/settings") &&
          r.request().method() === "PUT",
      ),
      page.getByRole("button", { name: "保存调度设置", exact: true }).click(),
    ]);
    assert.equal(saved.request().postDataJSON().pool_workers["x.tabbit"], 3);
    assert.equal((await saved.json()).pool_workers["x.tabbit"], 3);
    assert.equal(
      (
        await page.evaluate(() =>
          fetch("/api/execution/settings").then((r) => r.json()),
        )
      ).pool_workers["x.tabbit"],
      3,
    );
    checks.push({
      scenario: "slow-put-keeps-reopened-edit",
      failure,
      close,
      width: viewport.width,
      iteration,
    });
  } finally {
    release.resolve();
    await context.close();
  }
}
async function currentFailure(base, viewport, method) {
  const { context, page } = await pageFor(base, viewport);
  try {
    await page.route("**/api/execution/settings", async (route) => {
      if (route.request().method() === method)
        await route.fulfill({
          status: 500,
          json: { error: "synthetic-current-failure" },
        });
      else await route.continue();
    });
    await page.getByRole("button", { name: "调度与额度", exact: true }).click();
    if (method === "PUT")
      await page
        .getByRole("button", { name: "保存调度设置", exact: true })
        .click();
    await page.waitForFunction(
      () =>
        document.querySelector("#notice").textContent ===
        "synthetic-current-failure",
    );
    assert.equal(
      await page.locator("#panel").evaluate((el) => el.open),
      method === "PUT",
    );
    if (method === "PUT")
      assert.equal(
        await page.getByRole("alert").textContent(),
        "synthetic-current-failure",
      );
    checks.push({
      scenario: "current-error-remains-visible",
      method,
      width: viewport.width,
    });
  } finally {
    await context.close();
  }
}
(async () => {
  execFileSync(
    python,
    ["-m", "auto_promotion.cli", "--db", path.join(temp, "test.db"), "init"],
    { cwd: root, env, stdio: "pipe" },
  );
  const base = await new Promise((resolve, reject) => {
    server = spawn(
      python,
      [
        "-m",
        "auto_promotion.cli",
        "--db",
        path.join(temp, "test.db"),
        "serve",
        "--port",
        "0",
        "--no-worker",
      ],
      { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] },
    );
    timer = setTimeout(
      () => reject(Error("temporary simulation server startup timeout")),
      20000,
    );
    server.once("error", reject);
    server.once("exit", (code) =>
      reject(Error("temporary simulation server exited: " + code)),
    );
    server.stderr.on("data", (data) => {
      const match = data
        .toString()
        .match(/Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/);
      if (match) {
        clearTimeout(timer);
        resolve(match[1]);
      }
    });
  });
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.AP_UI_CHROMIUM_PATH || undefined,
  });
  const failures = [];
  for (const viewport of [
    { width: 390, height: 844 },
    { width: 1440, height: 1050 },
  ]) {
    for (let iteration = 0; iteration < 3; iteration++)
      for (const failure of [false, true])
        for (const [scenario, option] of [
          [slowGet, false],
          [slowGet, true],
          [slowPut, "button"],
          [slowPut, "escape"],
          [slowPut, "synchronous"],
        ]) {
          try {
            await scenario(base, viewport, iteration, failure, option);
          } catch (error) {
            failures.push({
              scenario: scenario.name,
              width: viewport.width,
              iteration,
              failure,
              option,
              error: error.message,
            });
          }
        }
    for (const method of ["GET", "PUT"])
      await currentFailure(base, viewport, method);
  }
  console.log(JSON.stringify({ passed: checks.length, checks, failures }));
  assert.deepEqual(failures, []);
})()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    clearTimeout(timer);
    if (browser) await browser.close();
    if (server && server.exitCode === null)
      await new Promise((resolve) => {
        server.once("exit", resolve);
        server.kill("SIGTERM");
      });
    assert.equal(path.dirname(temp), fs.realpathSync(os.tmpdir()));
    fs.rmSync(temp, { recursive: true, force: true });
  });

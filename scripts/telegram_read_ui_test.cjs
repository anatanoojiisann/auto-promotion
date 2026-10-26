/* Offline Telegram read settings: every HTTP request uses local fixtures. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const web = path.resolve(__dirname, "../src/auto_promotion/web");
const policy = {
  requests_per_window: 120,
  window_seconds: 3600,
  scan_rpc_limit: 40,
  messages_per_page: 100,
  groups_per_scan: 10,
  cache_ttl_seconds: 300,
};
const account = {
  id: "tg-safe",
  platform: "telegram",
  label: "Offline Telegram",
  mode: "live",
  status: "healthy",
  capabilities: ["send_message"],
  disabled: false,
  daily_limit: 5,
  hourly_limit: 2,
  min_interval: 120,
  health: {},
};
function gate() {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
}
let browser;
const passed = [];
async function scenario(name, options = {}) {
  const context = await browser.newContext({
    viewport: options.mobile
      ? { width: 390, height: 844 }
      : { width: 1440, height: 1050 },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(4000);
  let reads = 0,
    saves = 0;
  const captured = gate(),
    release = gate();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    assert.equal(url.hostname, "read-policy.test");
    if (url.pathname.startsWith("/assets/"))
      return route.fulfill({
        path: path.join(web, path.basename(url.pathname)),
      });
    if (url.pathname === "/")
      return route.fulfill({ path: path.join(web, "index.html") });
    if (url.pathname.endsWith("/telegram-reads")) {
      const saving = route.request().method() === "PUT";
      if (saving) {
        saves++;
        assert.equal(route.request().headers()["if-match"], "0");
        assert.deepEqual(route.request().postDataJSON(), {
          ...policy,
          requests_per_window: 80,
        });
      } else reads++;
      if ((saving && options.delaySave) || (!saving && options.delayRead)) {
        captured.resolve();
        await release.promise;
      }
      if (
        (options.failure && (saving || options.delayRead)) ||
        (options.stale && saving)
      )
        return route.fulfill({
          status: 409,
          json: {
            error: options.stale
              ? "TelegramReadPolicyRevisionChanged"
              : "SyntheticReadPolicyConflict",
          },
        });
      return route.fulfill({
        json: {
          account_id: account.id,
          revision: saving ? 1 : 0,
          policy: saving ? { ...policy, requests_per_window: 80 } : policy,
        },
      });
    }
    const fixtures = {
      "/api/session": {},
      "/api/accounts": [
        account,
        { ...account, id: "x-safe", platform: "x", label: "Offline X" },
      ],
      "/api/tasks": [],
      "/api/connections": [],
      "/api/overview": { live_writes: false, global_stop: true },
    };
    assert.ok(
      Object.hasOwn(fixtures, url.pathname),
      "Unexpected API (no health probe allowed): " + url.pathname,
    );
    return route.fulfill({ json: fixtures[url.pathname] });
  });
  try {
    await page.goto("http://read-policy.test/#accounts");
    await page.locator('[data-disable="tg-safe"]').waitFor();
    const open = page.getByRole("button", { name: "读取设置", exact: true });
    assert.equal(
      await open.count(),
      1,
      "Only Telegram accounts may configure reads",
    );
    if (options.delayRead) {
      await open.evaluate((el) => {
        window.readOpenButton = el;
        el.click();
        el.click();
      });
      await captured.promise;
      assert.equal(reads, 1);
      await page.getByRole("button", { name: "关闭", exact: true }).click();
      await page.getByRole("button", { name: "添加账号", exact: true }).click();
      await page
        .getByLabel("显示名称", { exact: true })
        .fill("Keep read draft");
      release.resolve();
      await page.waitForFunction(() => !window.readOpenButton.disabled);
      assert.equal(
        await page.getByLabel("显示名称", { exact: true }).inputValue(),
        "Keep read draft",
      );
      assert.equal(await page.getByRole("alert").textContent(), "");
      assert.equal(saves, 0);
    } else {
      await open.click();
      const budget = page.getByLabel("窗口读取预算", { exact: true });
      await budget.waitFor();
      assert.equal(await budget.inputValue(), "120");
      assert.equal(
        await page
          .getByLabel("每页消息数", { exact: true })
          .getAttribute("max"),
        "100",
      );
      assert.equal(
        await page
          .getByLabel("每轮扫描群数", { exact: true })
          .getAttribute("max"),
        "100",
      );
      assert.match(
        await page.locator("#panel-content").innerText(),
        /不代表平台许可/,
      );
      assert.match(
        await page.locator("#panel-content").innerText(),
        /本机内置执行器预算；旧外部 MCP 通道未覆盖；云端执行器需独立配置并独立统计，不跨设备合计限额/,
      );
      const save = page.getByRole("button", {
        name: "保存读取设置",
        exact: true,
      });
      if (options.mobile)
        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
          true,
        );
      if (options.cancel) {
        await page.keyboard.press("Escape");
        assert.equal(saves, 0);
      } else if (options.invalid) {
        await budget.fill("0");
        await save.click();
        assert.equal(await budget.evaluate((el) => el.validity.valid), false);
        assert.equal(saves, 0);
      } else {
        await budget.fill("80");
        if (options.delaySave) {
          await save.evaluate((el) => {
            window.readSaveButton = el;
            el.click();
            el.click();
          });
          await captured.promise;
          assert.equal(saves, 1);
          if (options.cancelResponse) {
            await page.keyboard.press("Escape");
            await page
              .getByRole("button", { name: "添加账号", exact: true })
              .click();
            await page
              .getByLabel("显示名称", { exact: true })
              .fill("Keep save draft");
          }
          release.resolve();
          await page.waitForFunction(() => !window.readSaveButton.disabled);
          if (options.cancelResponse) {
            assert.equal(
              await page.getByLabel("显示名称", { exact: true }).inputValue(),
              "Keep save draft",
            );
            assert.equal(await page.getByRole("alert").textContent(), "");
          }
        } else await save.click();
        if (!options.cancelResponse) {
          if (options.failure || options.stale)
            await page.getByRole("alert").waitFor();
          else
            await page.waitForFunction(
              () => !document.querySelector("#panel").open,
            );
        }
        assert.equal(saves, 1);
      }
    }
    assert.deepEqual(errors, []);
    passed.push(name);
  } finally {
    release.resolve();
    await context.close();
  }
}
(async () => {
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.AP_UI_CHROMIUM_PATH || undefined,
  });
  for (const [name, options] of [
    ["default-policy-save", {}],
    ["telegram-only-mobile", { mobile: true }],
    ["cancel-keeps-policy", { cancel: true }],
    ["invalid-budget-no-request", { invalid: true }],
    ["duplicate-save-once", { delaySave: true }],
    ["server-rejection-stays-open", { failure: true }],
    ["concurrent-revision-rejects-stale-save", { stale: true }],
    ["cancel-late-read-success", { delayRead: true }],
    ["cancel-late-read-failure", { delayRead: true, failure: true }],
    ["cancel-late-save-success", { delaySave: true, cancelResponse: true }],
    [
      "cancel-late-save-failure",
      { delaySave: true, cancelResponse: true, failure: true },
    ],
  ])
    await scenario(name, options);
  console.log(JSON.stringify({ passed: passed.length, scenarios: passed }));
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => {
    if (browser) await browser.close();
  });

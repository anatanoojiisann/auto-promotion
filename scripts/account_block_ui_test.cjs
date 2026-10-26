/* Offline browser contract: every request is fulfilled locally; no service/accounts. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const web = path.resolve(__dirname, "../src/auto_promotion/web");
const block = {
  reason: "x_challenge",
  since: 1700000000,
  generation: 1,
  affected_tasks: ["task-safe"],
  health_ok: false,
  health_checked_at: null,
};
const account = {
  id: "account-safe",
  platform: "x",
  label: "Offline test account",
  mode: "live",
  status: "healthy",
  capabilities: ["reply"],
  disabled: false,
  daily_limit: 5,
  hourly_limit: 2,
  min_interval: 120,
  checked_at: 1700000000,
  health: {},
  outbound_block: block,
};
function gate() {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
}
let browser;
const passed = [];
async function scenario(name, options = {}) {
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(4000);
  let checks = 0,
    resumes = 0,
    current = structuredClone(account);
  const captured = gate(),
    release = gate();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    assert.equal(
      url.hostname,
      "recovery.test",
      "No external network permitted",
    );
    if (url.pathname.startsWith("/assets/")) {
      const file = path.basename(url.pathname);
      return route.fulfill({ path: path.join(web, file) });
    }
    if (url.pathname === "/")
      return route.fulfill({ path: path.join(web, "index.html") });
    if (url.pathname.endsWith("/outbound-block/check")) {
      checks++;
      if (options.delay) captured.resolve();
      if (options.delay) await release.promise;
      if (options.failed)
        return route.fulfill({
          status: 409,
          json: { error: "AccountIdentityChanged" },
        });
      current.outbound_block = {
        ...block,
        health_ok: !options.unhealthy,
        health_checked_at: 1700000001,
      };
      return route.fulfill({ json: current });
    }
    if (url.pathname.endsWith("/outbound-block/resume")) {
      resumes++;
      assert.deepEqual(route.request().postDataJSON(), {
        generation: block.generation,
      });
      if (options.expired || options.newBlock)
        return route.fulfill({
          status: 409,
          json: {
            error: options.newBlock
              ? "AccountBlockGenerationChanged"
              : "AccountBlockHealthExpired",
          },
        });
      if (options.delayResume) {
        captured.resolve();
        await release.promise;
        if (options.failedResume)
          return route.fulfill({
            status: 409,
            json: { error: "AccountBlockGenerationChanged" },
          });
      }
      current.outbound_block = null;
      return route.fulfill({ json: current });
    }
    const fixtures = {
      "/api/session": {},
      "/api/accounts": [current],
      "/api/tasks": [],
      "/api/connections": [],
      "/api/overview": { live_writes: false, global_stop: true },
    };
    assert.ok(
      Object.hasOwn(fixtures, url.pathname),
      "Unexpected API " + url.pathname,
    );
    return route.fulfill({ json: fixtures[url.pathname] });
  });
  try {
    await page.goto("http://recovery.test/#accounts");
    await page
      .getByRole("button", { name: "处理外发暂停", exact: true })
      .click();
    await page.getByText("task-safe", { exact: true }).waitFor();
    const check = page.getByRole("button", {
      name: "只读检查身份与页面",
      exact: true,
    });
    const resume = page.getByRole("button", {
      name: "恢复后续外发",
      exact: true,
    });
    assert.equal(await resume.isDisabled(), true);
    if (options.cancelFirst) {
      await page.getByRole("button", { name: "关闭", exact: true }).click();
      assert.equal(checks, 0);
      assert.equal(resumes, 0);
    } else if (options.delay) {
      await check.evaluate((el) => {
        window.oldRecoveryCheck = el;
        el.click();
        el.click();
      });
      await captured.promise;
      assert.equal(checks, 1);
      await page.keyboard.press("Escape");
      await page.getByRole("button", { name: "添加账号", exact: true }).click();
      await page
        .getByLabel("显示名称", { exact: true })
        .fill("Keep this draft");
      release.resolve();
      await page.waitForFunction(() => !window.oldRecoveryCheck.disabled);
      assert.equal(
        await page.getByLabel("显示名称", { exact: true }).inputValue(),
        "Keep this draft",
      );
      assert.equal(await page.getByRole("alert").textContent(), "");
      assert.equal(resumes, 0);
    } else {
      await check.click();
      if (options.failed || options.unhealthy) {
        if (options.failed) await page.getByRole("alert").waitFor();
        else
          await page
            .getByText(
              "只读检查未通过，账号外发仍暂停。请人工检查后重新验证。",
              { exact: true },
            )
            .waitFor();
        assert.equal(await resume.isDisabled(), true);
        assert.equal(resumes, 0);
      } else {
        await resume.waitFor({ state: "visible" });
        await page.waitForFunction(
          () => !document.querySelector("#resume-outbound-block").disabled,
        );
        if (options.delayResume) {
          await resume.evaluate((el) => {
            window.oldRecoveryResume = el;
            el.click();
            el.click();
          });
          await captured.promise;
          assert.equal(resumes, 1);
          if (options.cancelResume) {
            await page.keyboard.press("Escape");
            await page
              .getByRole("button", { name: "添加账号", exact: true })
              .click();
            await page
              .getByLabel("显示名称", { exact: true })
              .fill("Keep this resume draft");
          }
          release.resolve();
          if (options.cancelResume) {
            await page.waitForFunction(
              () => !window.oldRecoveryResume.disabled,
            );
            assert.equal(
              await page.getByLabel("显示名称", { exact: true }).inputValue(),
              "Keep this resume draft",
            );
            assert.equal(await page.getByRole("alert").textContent(), "");
          }
        } else await resume.click();
        if (options.expired || options.newBlock) {
          await page.getByRole("alert").waitFor();
          assert.equal(await resume.isDisabled(), true);
        } else if (!options.cancelResume)
          await page.waitForFunction(
            () => !document.querySelector("#panel").open,
          );
        assert.equal(checks, 1);
        assert.equal(resumes, 1);
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
    ["explicit-health-then-resume", {}],
    ["cancel-before-check", { cancelFirst: true }],
    ["duplicate-check-and-cancel-success", { delay: true }],
    ["duplicate-check-and-cancel-failure", { delay: true, failed: true }],
    ["identity-failure-keeps-paused", { failed: true }],
    ["expired-check-keeps-paused", { expired: true }],
    ["duplicate-resume-sends-once", { delayResume: true }],
    ["health-not-ok-keeps-paused", { unhealthy: true }],
    ["new-block-rejects-checked-generation", { newBlock: true }],
    [
      "cancel-resume-response-success",
      { delayResume: true, cancelResume: true },
    ],
    [
      "cancel-resume-response-failure",
      { delayResume: true, cancelResume: true, failedResume: true },
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

/* Synthetic task-start races: every request is fulfilled locally. */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");
const web = path.resolve(__dirname, "../src/auto_promotion/web");
const account = {
  id: "account-fixture",
  label: "Synthetic",
  platform: "telegram",
  mode: "simulation",
  status: "healthy",
  capabilities: ["send_message"],
  daily_limit: 5,
  hourly_limit: 2,
  min_interval: 0,
};
function task(id = "task-fixture") {
  return {
    id,
    name: id,
    mode: "simulation",
    state: "active",
    phase: "ready",
    account_ids: [account.id],
    account_snapshot: [account],
    manifest_hash: "frozen-fixture",
    manifest: { synthetic: true },
    next_at: 0,
    policy: {
      max_joins: 0,
      starts_at: "2026-10-26T00:00:00Z",
      ends_at: "2026-10-27T00:00:00Z",
      min_interval: 0,
    },
    items: [
      {
        id: id + "-item",
        position: 0,
        status: "not_attempted",
        external_reference: null,
        result: {},
        due_at: 1793059200,
        payload: {
          account_id: account.id,
          account_label: account.label,
          platform: "telegram",
          target: "@synthetic",
          action: "send_message",
          text: "Fixture only",
          scheduled_at: "2026-10-27T00:00:00Z",
          media: [],
        },
      },
    ],
    candidates: [],
    execution: { status: "unmonitored", pools: {} },
    channel: { requested: "inherit", default: "cloud", accounts: [] },
    readiness: {
      blockers: [],
      items: [],
      workflow: {
        status: "waiting_schedule",
        label: "等待计划时间",
        message: "任务已启动，尚未到计划时间。",
        next_at: 1793059200,
        can_prepare: false,
        counts: {
          planned: 1,
          not_attempted: 1,
          verified_sent: 0,
          membership_verified: 0,
        },
      },
    },
  };
}
function gate() {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
}
let browser;
const passed = [];
async function scenario(name, options = {}) {
  const context = await browser.newContext();
  await context.addInitScript(() => {
    const original = window.setInterval;
    window.setInterval = (fn, delay, ...args) => {
      if (delay === 10000) {
        window.executionPollTick = fn;
        return 0;
      }
      return original(fn, delay, ...args);
    };
  });
  const page = await context.newPage();
  page.setDefaultTimeout(4000);
  const captured = gate(),
    release = gate();
  let starts = 0,
    holdRead = false,
    heldRead = false;
  let current = task();
  if (options.intentDisplay)
    current.readiness.items = [
      {
        item_id: current.items[0].id,
        reason: "interaction_already_claimed",
        message: "该互动已由另一任务占用",
        blocked_by_task_id: "task-owner",
        blocked_by_item_id: "item-owner",
      },
    ];
  if (options.serviceDisplay || options.scopeFailureDisplay)
    current.readiness.workflow = {
      ...current.readiness.workflow,
      status: "waiting_execution_service",
      label: "等待执行服务恢复",
      message: "服务受限，原计划时间仍保留。",
      next_at: 1793000060,
      next_at_kind: "service_retry",
      schedule_next_at: 1793059200,
      service_observation: {
        task_scope_usable: false,
        task_at: 1793000000,
        global_state: "CloudWriteQuotaExceeded",
        global_at: 1793000010,
      },
    };
  if (options.scopeFailureDisplay)
    current.readiness.workflow.service_observation = {
      task_scope_usable: false,
      task_scope_failed: true,
      task_state: "FrozenJobChanged",
      task_at: 1793000010,
      task_retry_at: 1793000060,
      global_state: "connected",
      global_at: 1793000000,
    };
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    assert.equal(url.hostname, "task-race.test");
    if (url.pathname.startsWith("/assets/"))
      return route.fulfill({
        path: path.join(web, path.basename(url.pathname)),
      });
    if (url.pathname === "/")
      return route.fulfill({ path: path.join(web, "index.html") });
    if (
      url.pathname === "/api/task-templates" &&
      route.request().method() === "POST"
    ) {
      captured.resolve();
      await release.promise;
      if (options.failure)
        return route.fulfill({
          status: 409,
          json: { error: "SyntheticTemplateConflict" },
        });
      return route.fulfill({ json: { id: "template-fixture" } });
    }
    if (url.pathname.endsWith("/start")) {
      starts++;
      captured.resolve();
      await release.promise;
      if (options.failure)
        return route.fulfill({
          status: 409,
          json: { error: "SyntheticStartGateBlocked" },
        });
      const response = structuredClone(current);
      response.execution_result = { processed: [], joins: [] };
      if (options.currentGate) {
        response.readiness.workflow.message = "Stale success message";
        current.readiness.workflow = {
          ...current.readiness.workflow,
          status: "global_stop",
          label: "全局执行已停止",
          message: "全局执行已停止",
        };
      }
      return route.fulfill({ json: response });
    }
    if (url.pathname.startsWith("/api/tasks/")) {
      const id = url.pathname.split("/").at(-1);
      const snapshot = structuredClone(id === current.id ? current : task(id));
      if (holdRead && !heldRead) {
        heldRead = true;
        captured.resolve();
        await release.promise;
        if (options.pollFailure || options.pollSameRoute)
          return route.fulfill({
            status: 500,
            json: { error: "SyntheticPollFailure" },
          });
      }
      return route.fulfill({ json: snapshot });
    }
    const fixtures = {
      "/api/session": {},
      "/api/accounts": [account],
      "/api/overview": { live_writes: false, global_stop: false },
      "/api/tasks": [current],
      "/api/connections": [],
      "/api/audit": [],
    };
    assert.ok(
      Object.hasOwn(fixtures, url.pathname),
      "Unexpected API " + url.pathname,
    );
    return route.fulfill({ json: fixtures[url.pathname] });
  });
  try {
    await page.goto("http://task-race.test/#tasks/task-fixture");
    await page.locator("#run-task").waitFor();
    if (options.templateSave) {
      await page.locator("#save-task-template").click();
      await page
        .getByRole("button", { name: "保存模板", exact: true })
        .evaluate((el) => {
          window.oldSaveTemplate = el;
          el.click();
          el.click();
        });
      await captured.promise;
      await page.keyboard.press("Escape");
      await page.locator("#save-task-template").click();
      await page
        .getByLabel("模板名称", { exact: true })
        .fill("Keep later template");
      release.resolve();
      await page.waitForFunction(() => !window.oldSaveTemplate.disabled);
      assert.equal(
        await page.locator("#panel").evaluate((el) => el.open),
        true,
      );
      assert.equal(
        await page.getByLabel("模板名称", { exact: true }).inputValue(),
        "Keep later template",
      );
      assert.equal(
        (await page.getByRole("alert").allTextContents()).join(""),
        "",
      );
      assert.equal(await page.locator("#notice").textContent(), "");
    } else if (options.intentDisplay) {
      assert.match(
        await page.locator("#task-readiness").innerText(),
        /task-owner/,
      );
      assert.match(
        await page.locator("#task-readiness").innerText(),
        /item-owner/,
      );
    } else if (options.serviceDisplay || options.scopeFailureDisplay) {
      const message = await page.locator("#task-workflow").innerText();
      assert.match(message, /下次服务检查/);
      assert.match(message, /原定下一计划时间/);
      assert.match(message, /状态观测不替代提交前授权/);
      if (options.scopeFailureDisplay) {
        assert.match(message, /当前同步失败（FrozenJobChanged）/);
        assert.match(message, /本任务同步重试时间/);
      } else assert.match(message, /CloudWriteQuotaExceeded/);
    } else if (
      options.reorder ||
      options.pollFailure ||
      options.pollSuccess ||
      options.pollSameRoute
    ) {
      holdRead = true;
      current.name = "Old snapshot";
      const first =
        options.pollFailure || options.pollSuccess || options.pollSameRoute
          ? page.evaluate(() => window.executionPollTick())
          : page.evaluate(() => renderTask("task-fixture"));
      await captured.promise;
      if (options.pollFailure) {
        await page.evaluate(() => {
          location.hash = "tasks/task-other";
        });
        await page
          .getByRole("heading", { name: "task-other", exact: true })
          .waitFor();
      } else {
        current.name = "New snapshot";
        current.readiness.workflow.message = "Newer scope state";
        await page.evaluate(() => renderTask("task-fixture"));
      }
      release.resolve();
      await first;
      if (options.pollFailure)
        assert.doesNotMatch(
          await page.locator("#task-execution").innerText(),
          /暂时无法刷新/,
        );
      else if (options.pollSuccess || options.pollSameRoute) {
        assert.match(
          await page.locator("#task-workflow").innerText(),
          /Newer scope state/,
        );
        assert.doesNotMatch(
          await page.locator("#task-execution").innerText(),
          /暂时无法刷新/,
        );
      } else
        assert.equal(
          await page
            .getByRole("heading", { name: "New snapshot", exact: true })
            .count(),
          1,
        );
    } else {
      await page.locator("#run-task").evaluate((el) => {
        window.oldStartButton = el;
        el.click();
      });
      await captured.promise;
      if (options.redraw) {
        await page.locator("#refresh-task").click();
        await page.locator("#run-task").waitFor();
        assert.equal(
          await page.locator("#run-task").isDisabled(),
          true,
          "Redraw must retain pending task start",
        );
        await page.locator("#run-task").evaluate((el) => el.click());
        assert.equal(starts, 1);
      }
      if (options.navigate) {
        await page.getByRole("link", { name: "账号中心", exact: true }).click();
        await page
          .getByRole("button", { name: "添加账号", exact: true })
          .click();
        await page
          .getByLabel("显示名称", { exact: true })
          .fill("Preserve new draft");
      } else if (options.panel) {
        await page.locator("#save-task-template").click();
        await page
          .getByLabel("模板名称", { exact: true })
          .fill("Preserve template draft");
      }
      release.resolve();
      await page.waitForFunction(() => !window.oldStartButton.disabled);
      if (options.navigate || options.panel) {
        assert.equal(
          (await page.getByRole("alert").allTextContents()).join(""),
          "",
        );
        assert.equal(await page.locator("#notice").textContent(), "");
        assert.equal(
          await page
            .getByLabel(options.navigate ? "显示名称" : "模板名称", {
              exact: true,
            })
            .inputValue(),
          options.navigate ? "Preserve new draft" : "Preserve template draft",
        );
      } else {
        await page.waitForFunction(
          () => document.querySelector("#notice").textContent.length > 0,
        );
        assert.equal(
          await page.locator("#notice").textContent(),
          options.currentGate
            ? "全局执行已停止"
            : "任务已启动，尚未到计划时间。",
        );
      }
      assert.equal(starts, 1);
    }
    assert.deepEqual(pageErrors, []);
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
  const failures = [];
  for (const [name, options] of [
    ["pending-start-survives-redraw", { redraw: true }],
    ["late-start-success-keeps-new-route-draft", { navigate: true }],
    [
      "late-start-failure-keeps-new-route-draft",
      { navigate: true, failure: true },
    ],
    ["late-start-success-keeps-new-panel", { panel: true }],
    ["late-start-failure-keeps-new-panel", { panel: true, failure: true }],
    ["start-shows-current-gate", { currentGate: true }],
    ["start-shows-future-schedule", {}],
    ["blocking-intent-owner-is-visible", { intentDisplay: true }],
    ["late-template-save-keeps-new-draft", { templateSave: true }],
    [
      "late-template-failure-keeps-new-draft",
      { templateSave: true, failure: true },
    ],
    [
      "service-retry-and-send-schedule-shown-separately",
      { serviceDisplay: true },
    ],
    [
      "fresh-own-sync-failure-shown-over-global-success",
      { scopeFailureDisplay: true },
    ],
    ["old-task-response-cannot-replace-newer", { reorder: true }],
    ["old-poll-failure-cannot-replace-other-task", { pollFailure: true }],
    ["old-poll-success-cannot-replace-newer-scope", { pollSuccess: true }],
    [
      "old-poll-failure-cannot-replace-same-route-refresh",
      { pollSameRoute: true },
    ],
  ]) {
    try {
      await scenario(name, options);
    } catch (error) {
      failures.push({ name, error: error.message });
    }
  }
  console.log(
    JSON.stringify({ passed: passed.length, scenarios: passed, failures }),
  );
  assert.deepEqual(failures, []);
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => {
    if (browser) await browser.close();
  });

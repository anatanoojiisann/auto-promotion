/* Real HTTP/UI acceptance in a temporary store; no real account or external endpoint. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, execFileSync } = require("node:child_process");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "..");
const temp = fs.realpathSync(
  fs.mkdtempSync(path.join(os.tmpdir(), "ap-hf-ui-")),
);
const db = path.join(temp, "hf.db");
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
  PYTHONPATH: path.join(root, "src"),
  PYTHONIOENCODING: "utf-8",
  HOME: temp,
  AP_DB: db,
  AP_ACCOUNTS_DIR: path.join(temp, "accounts"),
  AP_API_TOKEN: "synthetic-hf-ui-token",
  AUTO_PROMOTION_WORKSPACE: temp,
  AUTO_PROMOTION_LIVE_WRITES: "0",
  AP_CLOUD_CONTROL_URL: "",
  AP_CLOUD_NODE_TOKEN: "",
  AP_CLOUD_OPERATOR_TOKEN: "",
  AP_CLOUD_OPERATOR_TOKEN_FILE: "",
  AP_DRAFT_ENDPOINT: "",
};
for (const key of ["SYSTEMROOT", "WINDIR", "TEMP", "TMP"])
  if (process.env[key]) env[key] = process.env[key];
let server, browser, page;
const errors = [];
async function cleanup() {
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    const exited = new Promise((resolve) => server.once("exit", resolve));
    server.kill("SIGTERM");
    await Promise.race([
      exited,
      new Promise((resolve) => setTimeout(resolve, 3000)),
    ]);
  }
  assert.equal(path.dirname(temp), fs.realpathSync(os.tmpdir()));
  fs.rmSync(temp, { recursive: true, force: true });
}
(async () => {
  const seed = `
import time
from pathlib import Path
from auto_promotion.core import Engine
from auto_promotion.store import Store
from auto_promotion.models import AccountInput,TaskInput,CandidateInput
from auto_promotion.interaction_models import InteractionConfig,ConfigSave
from auto_promotion.interaction_simulation import OfflineRegistry,SimulationConnectors,source
from auto_promotion.interactions import Interactions
engine=Engine(Store(${JSON.stringify(db)}),OfflineRegistry(Path(${JSON.stringify(temp)})))
a=engine.add_account(AccountInput(platform='x',mode='simulation',label='UI simulated X',backend_label='hf-ui'))
engine.check_account(a['id'])
secondary=engine.add_account(AccountInput(platform='x',mode='simulation',label='A UI secondary official',backend_label='hf-secondary'))
engine.check_account(secondary['id'])
browser=engine.add_account(AccountInput(platform='x',mode='simulation',label='UI browser cannot scan API',backend_label='hf-browser'))
with engine.store.connect(write=True) as db:
    db.execute("UPDATE accounts SET mode='live' WHERE id=?",(browser['id'],))
io=SimulationConnectors(engine)
s=Interactions(engine,connectors=io)
now=time.time()
legacy=engine.create_task(TaskInput(name='Saved public discovery',account_ids=[a['id']],policy={'project_id':'ui-acceptance','starts_at':__import__('datetime').datetime.fromtimestamp(now-1,__import__('datetime').timezone.utc).isoformat(),'ends_at':__import__('datetime').datetime.fromtimestamp(now+86400,__import__('datetime').timezone.utc).isoformat()}))
engine.candidate(legacy['id'],CandidateInput(account_id=a['id'],target='https://x.com/example/status/9199',text='A saved public question from the same project',is_public=True))
cfg=InteractionConfig(project_id='ui-acceptance',enabled=True,account_ids=[a['id'],secondary['id']],queries=['synthetic'],facts=[{'id':'demo-fact','text':'Synthetic fact','source':'simulation://fact','verified_at':now-10,'valid_until':now+86400}])
p=s.save(ConfigSave(config=cfg))
io.sources=[source(9101,'P2',now),source(9102,'P2',now)]
s.trigger(p['id'],manual=True)
s.run_stage('scan')
s.run_stage('classify')
s.settle_rounds()
print(p['id'])
`;
  const id = execFileSync(python, ["-c", seed], {
    cwd: root,
    env,
    encoding: "utf8",
  }).trim();
  const url = await new Promise((resolve, reject) => {
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
      { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] },
    );
    const timer = setTimeout(
      () => reject(new Error("HTTP server startup timeout")),
      20000,
    );
    server.once("error", reject);
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
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.AP_UI_CHROMIUM_PATH || undefined,
  });
  page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(url + "/#interactions/" + id);
  await page.getByRole("heading", { name: "候选选择", exact: true }).waitFor();
  assert.equal(await page.locator("[data-hf-select]:enabled").count(), 2);
  const first = page.locator("[data-hf-select]:enabled").first();
  const cid = await first.getAttribute("data-hf-select");
  await page.locator(`[data-hf-edit="${cid}"]`).click();
  await page
    .locator("#hf-edit-form textarea")
    .fill("A precise human-edited answer preserved by the server.");
  await page.getByRole("button", { name: "保存编辑", exact: true }).click();
  await page.locator("#panel").waitFor({ state: "hidden" });
  await page.locator(`[data-hf-select="${cid}"]`).check();
  await page.getByRole("button", { name: "采用所选", exact: true }).click();
  await page.waitForFunction(
    () => document.querySelectorAll("[data-hf-select]:enabled").length === 1,
  );
  const backend = await page.evaluate(
    async (id) => api(`/interactions/${id}/candidates`),
    id,
  );
  assert.equal(backend.find((c) => c.id === cid).state, "QUEUED");
  assert.equal(
    backend.find((c) => c.id === cid).draft,
    "A precise human-edited answer preserved by the server.",
  );
  assert.equal(backend.filter((c) => c.state === "REVIEW_PENDING").length, 1);
  await page.getByRole("button", { name: "设置", exact: true }).click();
  const incompatible = page
    .locator(".hf-account")
    .filter({ hasText: "UI browser cannot scan API" });
  assert.equal(await incompatible.locator("input").isDisabled(), true);
  assert.match(await incompatible.textContent(), /connection_type=x_api/);
  assert.match(await incompatible.textContent(), /浏览器连接不能/);
  const beforeSettings = await page.evaluate(
    async (id) => api(`/interactions/${id}`),
    id,
  );
  const domSelected = await page
    .locator('#hf-settings-form [name="accounts"]:checked')
    .evaluateAll((els) => els.map((e) => e.value));
  assert.notDeepEqual(domSelected, beforeSettings.config.account_ids);
  await page.locator('#hf-settings-form [name="interval"]').fill("300");
  assert.match(await page.locator("#hf-calculation").textContent(), /25 分钟/);
  await page.getByRole("button", { name: "保存策略", exact: true }).click();
  await page.locator("#panel").waitFor({ state: "hidden" });
  const saved = await page.evaluate(
    async (id) => api(`/interactions/${id}`),
    id,
  );
  assert.equal(saved.config.scan.interval_seconds, 300);
  assert.deepEqual(saved.config.account_ids, beforeSettings.config.account_ids);
  assert.equal(saved.version, 2);
  await page.getByRole("button", { name: "扫描一次", exact: true }).click();
  await page.waitForFunction(() =>
    document.body.textContent.includes("collecting"),
  );
  assert.match(
    await page.locator("#hf-readiness").textContent(),
    /外部连接未验证/,
  );
  assert.match(
    await page.locator("#hf-delivery").textContent(),
    /结果未知.*禁止补发/,
  );
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page
    .locator(
      `#hf-settings-form [name="accounts"][value="${saved.config.account_ids[0]}"]`,
    )
    .uncheck();
  await page
    .locator('#hf-settings-form [name="source"]')
    .selectOption("imported_results");
  await page.getByRole("button", { name: "保存策略", exact: true }).click();
  await page.locator("#panel").waitFor({ state: "hidden" });
  const afterRemoval = await page.evaluate(
    async (id) => api(`/interactions/${id}`),
    id,
  );
  assert.deepEqual(afterRemoval.config.account_ids, [
    saved.config.account_ids[1],
  ]);
  await page.locator("#hf-import").click();
  await page
    .locator('#hf-import-form [name="author"]')
    .fill("synthetic-public-author");
  await page
    .locator('#hf-import-form [name="eventTime"]')
    .fill(new Date(Date.now() - 60000).toISOString().slice(0, 16));
  await page.getByRole("button", { name: "导入已有结果", exact: true }).click();
  await page.locator("#panel").waitFor({ state: "hidden" });
  await page.waitForFunction(() =>
    document
      .querySelector("#main")
      .textContent.includes("A saved public question from the same project"),
  );
  const imported = await page.evaluate(
    async (id) => api(`/interactions/${id}`),
    id,
  );
  assert.equal(imported.runs[0].coverage_incomplete, 1);
  assert.equal(imported.empty_streak, 0);
  assert.equal(await page.locator("#hf-scan").isDisabled(), true);
  fs.mkdirSync(path.join(root, "output/playwright"), { recursive: true });
  await page.screenshot({
    path: path.join(root, "output/playwright/hf-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: path.join(root, "output/playwright/hf-mobile.png"),
    fullPage: true,
  });
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
    "mobile document overflow",
  );
  await page.getByRole("button", { name: "全部策略", exact: true }).click();
  await page.getByRole("button", { name: "运行隔离模拟", exact: true }).click();
  await page.getByText("隔离模拟验收", { exact: true }).waitFor();
  assert.match(await page.locator("#panel-content").textContent(), /通过/);
  assert.deepEqual(errors, []);
  fs.writeFileSync(
    path.join(root, "var/hf-ui-test-result.json"),
    JSON.stringify(
      {
        passed: true,
        checks: [
          "real_backend_candidates",
          "human_edit_preserved",
          "selected_only_adoption",
          "settings_version_and_timing",
          "queued_scan",
          "desktop",
          "mobile_no_overflow",
          "isolated_e2e_modal",
          "no_browser_errors",
          "backend_readiness_and_delivery_summary",
          "saved_candidate_import_partial_coverage",
          "official_api_account_selection_blocks_browser_with_action",
          "reader_order_stable_until_explicit_removal",
        ],
      },
      null,
      2,
    ),
  );
  console.log(
    "High-frequency UI passed; 13 checks; desktop/mobile screenshots saved.",
  );
})()
  .catch(async (e) => {
    console.error(e);
    if (page) {
      console.error((await page.locator("body").innerText()).slice(-3000));
      fs.mkdirSync(path.join(root, "output/playwright"), { recursive: true });
      await page.screenshot({
        path: path.join(root, "output/playwright/hf-failure.png"),
        fullPage: true,
      });
    }
    process.exitCode = 1;
  })
  .finally(cleanup);

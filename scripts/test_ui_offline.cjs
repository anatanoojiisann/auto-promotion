/* Keep all UI children independent of the caller's accounts and environment. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const suites = {
  main: "ui_test.cjs",
  pools: "pool_ui_test.cjs",
  interactions: "hf_ui_test.cjs",
  faults: "ui_fault_test.cjs",
  blocks: "account_block_ui_test.cjs",
  telegram_reads: "telegram_read_ui_test.cjs",
  task_start: "task_start_ui_test.cjs",
};
const selected = process.argv.slice(2);
if (!selected.length) selected.push("main", "pools");
assert.ok(selected.every((key) => Object.hasOwn(suites, key)));
const temp = fs.realpathSync(
  fs.mkdtempSync(path.join(os.tmpdir(), "ap-ui-runner-")),
);
const env = {
  PATH: process.env.PATH,
  HOME: temp,
  TMPDIR: fs.realpathSync(os.tmpdir()),
  AP_DB: path.join(temp, "unused-default.db"),
  AP_ACCOUNTS_DIR: path.join(temp, "accounts"),
  AUTO_PROMOTION_WORKSPACE: temp,
  AUTO_PROMOTION_LIVE_WRITES: "0",
  PYTHONPATH: path.join(root, "src"),
  AUTO_PROMOTION_PYTHON:
    process.env.AUTO_PROMOTION_PYTHON ||
    path.join(
      root,
      process.platform === "win32"
        ? ".venv/Scripts/python.exe"
        : ".venv/bin/python",
    ),
  AP_UI_SKIP_SCREENSHOTS: "1",
};
if (process.env.AP_UI_CHROMIUM_PATH)
  env.AP_UI_CHROMIUM_PATH = process.env.AP_UI_CHROMIUM_PATH;
for (const key of ["SYSTEMROOT", "WINDIR", "TEMP", "TMP"])
  if (process.env[key]) env[key] = process.env[key];
let child;
(async () => {
  for (const suite of selected) {
    const code = await new Promise((resolve, reject) => {
      child = spawn(process.execPath, [path.join(__dirname, suites[suite])], {
        cwd: root,
        env,
        stdio: "inherit",
      });
      child.once("error", reject);
      child.once("close", (status) => resolve(status ?? 1));
    });
    if (code) {
      process.exitCode = code;
      return;
    }
  }
})()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(() => {
    assert.ok(!child || child.exitCode !== null || child.signalCode !== null);
    assert.equal(path.dirname(temp), fs.realpathSync(os.tmpdir()));
    fs.rmSync(temp, { recursive: true, force: true });
  });

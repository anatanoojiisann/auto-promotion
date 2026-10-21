/* Complete offline Python suite: isolated HOME/profiles, Chromium and local Miniflare. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createRequire } = require("node:module");
const { spawn } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const cloudRoot = path.join(root, "cloudflare/control-plane");
const { Miniflare, Log, LogLevel, convertV4MiniflareOptions } = createRequire(
  path.join(cloudRoot, "package.json"),
)("miniflare");
const { chromium } = require("playwright");
const temp = fs.realpathSync(
  fs.mkdtempSync(path.join(os.tmpdir(), "ap-offline-suite-")),
);
let mf, child, timer;
(async () => {
  mf = new Miniflare(
    convertV4MiniflareOptions({
      name: "offline-python-tests",
      modules: [
        "worker.mjs",
        "kernel.mjs",
        "schema.mjs",
        "maintenance.mjs",
        "standby.mjs",
        "offline-permit.mjs",
        "workflow-queue.mjs",
        "public-research.mjs",
        "telegram-executor-bridge.mjs",
        "telegram-native-gate.mjs",
        "telegram-workflow-reader.mjs",
        "telegram-prepared-replies.mjs",
        "telegram-reply-policy.mjs",
        "telegram-task-builder.mjs",
      ].map((name) => ({
        type: "ESModule",
        path: path.join(cloudRoot, name),
        contents: fs.readFileSync(path.join(cloudRoot, name), "utf8"),
      })),
      modulesRoot: cloudRoot,
      compatibilityDate: "2025-09-01",
      // Synthetic fixture value expected by test_cloud_control.py, never a user credential.
      bindings: {
        OPERATOR_TOKEN: "test-control-only",
        WORKSPACE_ID: "isolated-test",
      },
      durableObjects: {
        CONTROL: { className: "WorkspaceCoordinator", useSQLite: true },
      },
      cf: false,
      log: new Log(LogLevel.ERROR),
      port: 0,
    }),
  );
  const url = await mf.ready;
  const env = {
    PATH: process.env.PATH,
    HOME: temp,
    TMPDIR: fs.realpathSync(os.tmpdir()),
    PYTHONPATH: path.join(root, "src"),
    PYTHONIOENCODING: "utf-8",
    AP_DB: path.join(temp, "unused-default.db"),
    AP_ACCOUNTS_DIR: path.join(temp, "accounts"),
    AUTO_PROMOTION_WORKSPACE: temp,
    AUTO_PROMOTION_LIVE_WRITES: "0",
    AP_TEST_CONTROL_URL: url.toString(),
    AP_TEST_BROWSER_BACKENDS: "1",
    AP_BROWSER_EXECUTABLE:
      process.env.AP_BROWSER_EXECUTABLE || chromium.executablePath(),
  };
  for (const key of ["SYSTEMROOT", "WINDIR", "TEMP", "TMP"])
    if (process.env[key]) env[key] = process.env[key];
  const python =
    process.env.AUTO_PROMOTION_PYTHON ||
    path.join(
      root,
      process.platform === "win32"
        ? ".venv/Scripts/python.exe"
        : ".venv/bin/python",
    );
  const args = ["-m", "unittest", "discover", "-s", "tests", "-v"];
  if (process.argv[2]) args.push("-p", process.argv[2]);
  process.exitCode = await new Promise((resolve, reject) => {
    child = spawn(python, args, { cwd: root, env, stdio: "inherit" });
    child.once("error", reject);
    child.once("close", (code) => resolve(code ?? 1));
    timer = setTimeout(
      () => child.kill("SIGTERM"),
      Number(process.env.AP_TEST_TIMEOUT_MS || 300000),
    );
  });
})()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    clearTimeout(timer);
    if (child && child.exitCode === null) child.kill("SIGTERM");
    if (mf) await mf.dispose();
    assert.equal(path.dirname(temp), fs.realpathSync(os.tmpdir()));
    fs.rmSync(temp, { recursive: true, force: true });
  });

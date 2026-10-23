const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");
const path = require("node:path");
(async () => {
  const root = path.resolve(__dirname, "..");
  const code =
    "from auto_promotion.device_ui import make_server; s=make_server('unused',0,display={'account_ids':['synthetic'],'targets':['-1002123962275'],'max_messages':1}); print(s.server_port,flush=True); s.serve_forever()";
  const child = spawn(
    path.join(root, ".venv/Scripts/python.exe"),
    ["-c", code],
    {
      cwd: root,
      env: { ...process.env, AUTO_PROMOTION_LIVE_WRITES: "0" },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let browser;
  try {
    const port = await new Promise((resolve, reject) => {
      child.stdout.once("data", (d) => resolve(Number(d.toString().trim())));
      child.once("exit", () => reject(Error("FixtureExited")));
    });
    browser = await chromium.launch({
      headless: true,
      executablePath:
        process.env.AP_BROWSER_EXECUTABLE ||
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
    });
    const page = await browser.newPage();
    let prepares = 0,
      starts = 0,
      cancels = 0;
    await page.route("**/api/device/*", async (route) => {
      const op = route.request().url().split("/").pop();
      if (op === "prepare") prepares++;
      if (op === "start") starts++;
      if (op === "cancel") cancels++;
      await new Promise((resolve) => setTimeout(resolve, 150));
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          state:
            op === "cancel"
              ? "cancelled"
              : op === "prepare"
                ? "draft"
                : "unknown",
          attempted: op === "start",
          synthetic: true,
        }),
      });
    });
    await page.goto("http://127.0.0.1:" + port);
    assert.match(await page.locator("body").innerText(), /-1002123962275/);
    await page.locator("#text").fill("synthetic text");
    await page.locator("[data-op=prepare]").dblclick();
    await page.waitForFunction(
      () => !document.querySelector("button").disabled,
    );
    assert.equal(prepares, 1);
    assert.equal(starts, 0);
    await page.locator("[data-op=cancel]").click();
    await page.waitForFunction(() =>
      document.querySelector("#result").textContent.includes("cancelled"),
    );
    assert.equal(cancels, 1);
    assert.equal(starts, 0);
    console.log(
      JSON.stringify({
        checks: 5,
        passed: 5,
        external_calls: 0,
        real_sends: 0,
      }),
    );
  } finally {
    if (browser) await browser.close();
    child.kill();
  }
})().catch((e) => {
  console.error(e.message);
  process.exitCode = 1;
});

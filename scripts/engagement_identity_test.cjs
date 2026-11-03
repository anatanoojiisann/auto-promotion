const { chromium } = require("playwright");
const { expect } = require("playwright/test");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");

const reference = "https://x.com/brand/status/123456";
const python =
  process.env.AUTO_PROMOTION_PYTHON ||
  path.resolve(
    process.platform === "win32"
      ? ".venv/Scripts/python.exe"
      : ".venv/bin/python",
  );
const program = execFileSync(
  python,
  [
    "-c",
    "from auto_promotion.backends.engagement import tabbit_metrics_program; print(tabbit_metrics_program('brand','https://x.com/brand/status/123456','123456'))",
  ],
  { encoding: "utf8", env: { ...process.env, PYTHONIOENCODING: "utf-8" } },
);
const run = new (Object.getPrototypeOf(async function () {}).constructor)(
  "page",
  "expect",
  "assert",
  program,
);

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    for (const test of [
      { menu: "brand", profile: "brand", expected: true },
      { menu: "other", profile: "brand", expected: false },
      { menu: "brand", profile: "other", expected: false },
    ]) {
      const page = await browser.newPage({
        viewport: { width: 1022, height: 800 },
      });
      await page.route("https://x.com/**", (route) =>
        route.fulfill({
          contentType: "text/html",
          body: `<!doctype html><button data-testid="SideNav_AccountSwitcher_Button" aria-label="Account menu"></button><a href="/${test.profile}" aria-label="Profile">Profile</a><article data-testid="tweet"><a href="/brand/status/123456"><time>now</time></a><a href="/brand/status/123456/analytics" aria-label="18 Views">18</a><button data-testid="like" aria-label="2 Likes"></button><button data-testid="reply" aria-label="3 Replies"></button><button data-testid="retweet" aria-label="0 Reposts"></button></article><script>const menu=document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');const render=()=>menu.textContent=innerWidth>=1280?'Brand @${test.menu}':'';window.addEventListener('resize',render);setTimeout(render,30);</script>`,
        }),
      );
      if (test.expected) {
        const result = await run(page, expect, assert);
        assert.equal(result.subject, true);
        assert.equal(result.likes[0], "2 Likes");
        assert.equal(result.views[0], "18 Views");
      } else {
        await assert.rejects(
          () => run(page, expect, assert),
          /XIdentityMismatch/,
        );
      }
      assert.equal(page.isClosed(), true);
    }
    console.log(
      JSON.stringify({
        passed: true,
        compact_layout_recovered: true,
        wrong_menu_rejected: true,
        wrong_profile_rejected: true,
      }),
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});

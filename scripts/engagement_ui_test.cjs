const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    await page.setContent(
      '<main id="main"><section id="engagement-metrics"></section></main>',
    );
    await page.addStyleTag({
      path: path.resolve("src/auto_promotion/web/style.css"),
    });
    await page.evaluate(() => {
      window.$ = (s) => document.querySelector(s);
      window.esc = (v) =>
        String(v).replace(
          /[&<>"']/g,
          (s) =>
            ({
              "&": "&amp;",
              "<": "&lt;",
              ">": "&gt;",
              '"': "&quot;",
              "'": "&#39;",
            })[s],
        );
      window.fmt = (v) => new Date(v * 1000).toLocaleString("zh-CN");
      window.toast = (v) => {
        throw new Error(v);
      };
      window.busy = async (_button, fn) => fn();
      window.api = async (url) => {
        const metrics = { views: 1200, likes: 0, replies: null };
        if (url.includes("/items/"))
          return {
            samples: [
              { observed_at: 1793716000, metrics, estimated: ["views"] },
            ],
          };
        return {
          total: 1,
          limit: 50,
          offset: 0,
          scheduler_running: true,
          totals: {
            views: {
              observed_total: 1200,
              available_items: 1,
              estimated_items: 1,
            },
            likes: {
              observed_total: 0,
              available_items: 1,
              estimated_items: 0,
            },
            replies: {
              observed_total: 0,
              available_items: 0,
              estimated_items: 0,
            },
          },
          items: [
            {
              item_id: "item-test",
              account_label: "X · @our_account",
              external_reference: "https://x.com/our_account/status/123",
              metrics,
              estimated: ["views"],
              delta: { views: null, likes: 0, replies: null },
              observed_at: 1793716000,
              stale: false,
              status: "partial",
            },
          ],
        };
      };
    });
    await page.addScriptTag({
      path: path.resolve("src/auto_promotion/web/engagement.js"),
    });
    await page.evaluate(() => renderEngagement());
    const row = page.locator("#engagement-metrics tbody tr").first();
    assert.equal(
      (await row.locator("td").nth(1).textContent()).trim(),
      "约 1,200",
    );
    assert.match(await row.locator("td").nth(2).textContent(), /^0/);
    assert.equal(await row.locator("td").nth(3).textContent(), "不可获取");
    await page.getByRole("button", { name: "查看历史", exact: true }).click();
    await page.getByText("最近 1 次采集", { exact: true }).waitFor();
    fs.mkdirSync("output/playwright", { recursive: true });
    await page.screenshot({
      path: "output/playwright/engagement-desktop.png",
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
      path: "output/playwright/engagement-mobile.png",
      fullPage: true,
    });
    assert(
      await page
        .locator(".table-wrap")
        .evaluate((e) => e.scrollWidth > e.clientWidth),
    );

    // Read the actual extraction function against a thread containing the parent and our reply.
    const source = fs.readFileSync(
      "src/auto_promotion/backends/engagement.py",
      "utf8",
    );
    const extraction = source.match(/EXTRACT_JS = r"""([\s\S]*?)"""/)[1];
    await page.setContent(
      '<article data-testid="tweet"><a href="/parent/status/999"><time></time></a><button data-testid="like" aria-label="999 Likes"></button></article><article data-testid="tweet" id="ours"><a href="/our_account/status/123"><time></time></a><a href="/our_account/status/123/analytics" aria-label="1,234 Views">1.2K</a><button data-testid="like" aria-label="0 Likes"></button><button data-testid="reply" aria-label="2 Replies"></button></article>',
    );
    const observed = await page
      .locator("#ours")
      .evaluate(new Function("return (" + extraction + ")")(), {
        id: "123",
        identity: "our_account",
      });
    assert.equal(observed.subject, true);
    assert.equal(observed.likes[0], "0 Likes");
    assert.equal(
      (
        await page
          .locator("#ours")
          .evaluate(new Function("return (" + extraction + ")")(), {
            id: "999",
            identity: "our_account",
          })
      ).subject,
      false,
    );
    console.log(
      JSON.stringify({
        passed: true,
        screenshots: [
          "output/playwright/engagement-desktop.png",
          "output/playwright/engagement-mobile.png",
        ],
      }),
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});

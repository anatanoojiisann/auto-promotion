// The exact production Tabbit program; every HTTP request is an offline fixture.
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "..");
const program = execFileSync(
  process.env.AP_PYTHON || "python3",
  [
    "-c",
    "from auto_promotion.backends.tiktok_desktop import PROGRAM; print(PROGRAM)",
  ],
  {
    cwd: root,
    env: { ...process.env, PYTHONPATH: path.join(root, "src") },
    encoding: "utf8",
  },
);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.AP_BROWSER_EXECUTABLE,
  });
  const passed = [];
  try {
    for (const mode of [
      "comment",
      "root-zero",
      "existing-draft",
      "reply",
      "wrong-video",
      "adjacent-video",
      "wrong-parent-video",
      "missing-parent",
      "wrong-editor-parent",
      "misleading-parent-container",
      "403",
      "media403",
      "iframe",
      "redirect",
      "challenge",
      "no-receipt",
      "wrong-receipt-parent",
      "late-old-response",
    ]) {
      const context = await browser.newContext({ serviceWorkers: "block" });
      await context.setOffline(true);
      let posts = 0,
        oldRoute;
      let signalOld;
      const oldStarted = new Promise((resolve) => {
        signalOld = resolve;
      });
      try {
        await context.route("**/*", async (route) => {
          const request = route.request(),
            u = new URL(request.url());
          assert.equal(u.hostname, "www.tiktok.com", "unexpected fixture host");
          if (u.pathname === "/api/comment/publish/") {
            posts++;
            assert.equal(request.method(), "POST");
            if (mode === "late-old-response" && u.searchParams.has("old")) {
              oldRoute = route;
              signalOld();
              return;
            }
            if (mode === "late-old-response") {
              await oldRoute.fulfill({
                contentType: "application/json",
                body: JSON.stringify({
                  comment: {
                    cid: "789",
                    aweme_id: "123",
                    text: "exact text",
                    reply_id: "",
                    user: { unique_id: "brand" },
                  },
                }),
              });
              return route.abort("failed");
            }
            return route.fulfill({
              contentType: "application/json",
              body: JSON.stringify({
                comment: {
                  cid: "789",
                  aweme_id: "123",
                  text: "exact text",
                  reply_id:
                    mode === "reply" || mode === "wrong-receipt-parent"
                      ? "456"
                      : mode === "root-zero"
                        ? "0"
                        : "",
                  user: { unique_id: "brand" },
                },
              }),
            });
          }
          assert.equal(request.method(), "GET");
          if (mode === "403")
            return route.fulfill({ status: 403, body: "Denied" });
          if (u.pathname === "/media")
            return route.fulfill({ status: 403, body: "media denied" });
          const vid = mode === "wrong-video" ? "999" : "123";
          const receipt =
            posts > 0 && mode !== "no-receipt"
              ? '<div data-comment-id="789" data-video-id="123" data-author="brand" data-parent-comment-id="' +
                (mode === "reply"
                  ? "456"
                  : mode === "wrong-receipt-parent"
                    ? "999"
                    : "") +
                '"><span data-e2e="comment-level-1">exact text</span><a href="/@target/video/123?comment_id=789">Permalink</a></div>'
              : "";
          const button = (attr) =>
            '<button data-e2e="' +
            attr +
            "\" onclick=\"fetch('/api/comment/publish/',{method:'POST'}).then(r=>r.json())\">Post</button>";
          let html =
            '<link rel="canonical" href="https://www.tiktok.com/@target/video/' +
            vid +
            '"><a data-e2e="nav-profile" href="/@brand">Profile</a><video style="width:300px;height:150px"></video>';
          html +=
            '<div data-e2e="comment-text"><div contenteditable="true"></div></div>' +
            button("comment-post");
          if (!["missing-parent", "misleading-parent-container"].includes(mode))
            html +=
              '<div data-comment-id="456" data-video-id="' +
              (mode === "wrong-parent-video" ? "999" : "123") +
              '"><span data-e2e="comment-level-1">question</span><button data-e2e="comment-reply-1">Reply</button>' +
              (mode === "wrong-editor-parent"
                ? ""
                : '<div data-e2e="comment-reply-text"><div contenteditable="true"></div></div>' +
                  button("comment-reply-post")) +
              "</div>";
          if (mode === "wrong-editor-parent")
            html +=
              '<div data-comment-id="999" data-video-id="123"><div data-e2e="comment-reply-text"><div contenteditable="true"></div></div>' +
              button("comment-reply-post") +
              "</div>";
          if (mode === "misleading-parent-container")
            html +=
              '<span><a href="/@target/video/123?comment_id=456">Parent A</a></span><section data-comment-id="999" data-video-id="123"><button data-e2e="comment-reply-1">Reply B</button><div data-e2e="comment-reply-text"><div contenteditable="true"></div></div>' +
              button("comment-reply-post") +
              "</section>";
          if (mode === "adjacent-video")
            html += '<video style="width:300px;height:150px"></video>';
          if (mode === "iframe") html += '<iframe src="about:blank"></iframe>';
          if (mode === "challenge")
            html += '<div data-e2e="captcha">Verify</div>';
          if (mode === "redirect")
            html +=
              '<script>history.replaceState(null,"","/@target/video/999")</script>';
          if (mode === "media403") html += '<img src="/media">';
          await route.fulfill({
            contentType: "text/html",
            body: html + receipt,
          });
        });
        const page = await context.newPage();
        // Fixture timing must tolerate shared CI load; production navigation remains bounded.
        page.setDefaultTimeout(3000);
        await page.goto("https://www.tiktok.com/@brand");
        const replies = [
          "reply",
          "wrong-parent-video",
          "missing-parent",
          "wrong-editor-parent",
          "wrong-receipt-parent",
          "misleading-parent-container",
        ];
        const item = {
          action: replies.includes(mode) ? "reply" : "comment",
          target: "https://www.tiktok.com/@target/video/123",
          tiktok_video_id: "123",
          tiktok_comment_id: replies.includes(mode) ? "456" : "",
          text: "exact text",
        };
        const run = (phase, receipt = {}) =>
          new AsyncFunction(
            "page",
            "phase",
            "item",
            "expected",
            "identityKey",
            "target",
            "textHash",
            "receipt",
            program,
          )(
            page,
            phase,
            item,
            "brand",
            "brand",
            item.target,
            "fixture-hash",
            receipt,
          );
        if (mode === "existing-draft")
          await page
            .locator("[contenteditable=true]")
            .first()
            .fill("user draft");
        const preflight = await run("preflight");
        if (mode === "existing-draft") {
          assert.equal(preflight.reason, "TikTokCommentDraftNotEmpty");
          assert.equal(
            await page.locator("[contenteditable=true]").first().innerText(),
            "user draft",
          );
          assert.equal(page.url(), "https://www.tiktok.com/@brand");
        }
        if (
          ![
            "comment",
            "root-zero",
            "reply",
            "media403",
            "no-receipt",
            "wrong-receipt-parent",
            "late-old-response",
          ].includes(mode)
        ) {
          assert.equal(preflight.prepared, undefined, mode);
          assert.equal(preflight.blocked, true, mode);
          assert.equal(posts, 0);
          passed.push(mode);
          continue;
        }
        assert.equal(preflight.prepared, true, mode);
        assert.equal(
          await page.locator("[contenteditable=true]").first().innerText(),
          "",
          "preflight must not fill",
        );
        assert.equal((await run("prepare")).prepared, true, mode);
        if (mode === "late-old-response") {
          await page.evaluate(() => {
            void fetch("/api/comment/publish/?old=1", { method: "POST" }).catch(
              () => {},
            );
          });
          await oldStarted;
          const receipt = await run("submit");
          assert.equal(receipt.comment_id, null);
          assert.equal(posts, 2);
          assert.equal((await run("verify", receipt)).verified, false);
          passed.push(mode);
          continue;
        }
        const receipt = await run("submit");
        assert.equal(posts, 1, mode + " " + JSON.stringify(receipt));
        assert.equal(receipt.comment_id, "789", mode);
        const result = await run("verify", receipt);
        if (mode === "no-receipt" || mode === "wrong-receipt-parent")
          assert.equal(result.verified, false, mode);
        else {
          assert.equal(result.verified, true, mode);
          assert.equal(result.comment_id, "789");
        }
        assert.equal(posts, 1, "verify must never submit");
        passed.push(mode);
      } finally {
        await context.close();
      }
    }
    console.log(JSON.stringify({ passed: passed.length, scenarios: passed }));
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});

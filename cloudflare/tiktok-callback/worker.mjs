const page = `<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TikTok 授权回调 · Auto Promotion</title>
<body>
<main>
<h1>TikTok 授权回调</h1>
<p>请复制浏览器地址栏中的完整地址，返回本机 Auto Promotion 工作台。</p>
<ol>
<li>打开「账号中心 → 连接 TikTok」。</li>
<li>将完整地址粘贴到「授权完成后的完整回调地址」。</li>
<li>点击「完成账号连接」，由本机工作台核验并交换授权令牌。</li>
</ol>
<p>如果 TikTok 显示取消授权或错误，请回到工作台重新生成授权链接。</p>
<p>此页面不保存授权码，也不会代替你完成账号连接。请勿将地址栏中的授权信息分享给他人。</p>
</main>
</body>
</html>`;

const headers = {
  "Cache-Control": "no-store",
  "Content-Security-Policy":
    "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Strict-Transport-Security": "max-age=31536000",
};

export default {
  async fetch(request) {
    const { pathname } = new URL(request.url);
    if (!["GET", "HEAD"].includes(request.method)) {
      return new Response("Method not allowed", {
        status: 405,
        headers: { ...headers, Allow: "GET, HEAD" },
      });
    }
    const head = request.method === "HEAD";
    if (pathname === "/health") {
      return new Response(
        head
          ? null
          : JSON.stringify({
              service: "auto-promotion-tiktok-callback",
              mode: "manual_handoff",
              version: 1,
            }),
        {
          headers: {
            ...headers,
            "Content-Type": "application/json; charset=utf-8",
          },
        },
      );
    }
    if (pathname === "/" || pathname === "/oauth/tiktok/") {
      // Keep auth_code and state in the address bar only. No logs, persistence,
      // analytics, redirects, remote assets, or token exchange in this Worker.
      return new Response(head ? null : page, {
        headers: { ...headers, "Content-Type": "text/html; charset=utf-8" },
      });
    }
    return new Response(head ? null : "Not found", { status: 404, headers });
  },
};

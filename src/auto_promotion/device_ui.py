"""Loopback UI for one registered device grant; never enrolls or rotates credentials."""

import argparse
import html
import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from .device_control import SAFE_ERRORS, execute, load

PAGE = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>Telegram 单条测试</title>
<style>body{max-width:680px;margin:3rem auto;font:18px system-ui}textarea{width:100%;height:120px}button{margin:12px 8px 12px 0;padding:10px}pre{white-space:pre-wrap}</style>
<h1>Telegram 单条文本</h1><pre>__SCOPE__</pre><p>仅使用已注册设备的账号与目标。准备不会发送；开始只执行这一条。结果不确定时查状态，不重复创建。</p>
<label>固定内容<textarea id="text" maxlength="4096"></textarea></label><br>
<button data-op="prepare">准备</button><button data-op="start">开始发送一次</button><button data-op="status">查询状态</button><button data-op="cancel">取消未执行任务</button><pre id="result"></pre>
<script>let busy=false;document.querySelectorAll('button').forEach(b=>b.onclick=async()=>{if(busy)return;busy=true;document.querySelectorAll('button').forEach(x=>x.disabled=true);try{let r=await fetch('/api/device/'+b.dataset.op,{method:'POST',headers:{'Content-Type':'application/json','X-Device-UI':'__CSRF__'},body:JSON.stringify({text:document.querySelector('#text').value})});document.querySelector('#result').textContent=JSON.stringify(await r.json(),null,2)}catch{document.querySelector('#result').textContent='连接结果不确定，请查询状态，不重复创建。'}finally{busy=false;document.querySelectorAll('button').forEach(x=>x.disabled=false)}})</script></html>"""


def make_server(config, port, operation=execute, display=None):
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value, html=False):
            raw = value.encode() if html else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header(
                "Content-Type", "text/html; charset=utf-8" if html else "application/json"
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def allowed(self):
            expected = "127.0.0.1:" + str(self.server.server_port)
            return (
                self.headers.get("Host") == expected
                and self.headers.get("Origin", "http://" + expected) == "http://" + expected
            )

        def do_GET(self):
            if not self.allowed() or self.path != "/":
                return self.reply(403, {"error": "LocalUIOnly"})
            self.reply(
                200,
                PAGE.replace("__CSRF__", csrf).replace(
                    "__SCOPE__", html.escape(json.dumps(display or {}, ensure_ascii=False))
                ),
                True,
            )

        def do_POST(self):
            command = self.path.removeprefix("/api/device/")
            if (
                not self.allowed()
                or self.headers.get("X-Device-UI") != csrf
                or command not in {"prepare", "start", "status", "cancel"}
            ):
                return self.reply(403, {"error": "DeviceUIRequestDenied"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 20000:
                    raise ValueError()
                body = json.loads(self.rfile.read(size))
                value = operation(
                    SimpleNamespace(
                        command=command,
                        config=config,
                        text=body.get("text"),
                        operator_token_file=None,
                    )
                )
                self.reply(200, value)
            except Exception as exc:
                code = str(exc)
                self.reply(
                    409,
                    {
                        "error": code
                        if code in SAFE_ERRORS
                        else "DeviceOperationUnavailableCheckStatus",
                        "retry_send": False,
                    },
                )

    return HTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=8755)
    args = parser.parse_args()
    with make_server(args.config, args.port, display=load(args.config)["scope"]) as server:
        print("http://127.0.0.1:" + str(server.server_port), flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()

"""Read-only workbench against a migration copy, with every live writer disabled."""

import argparse
import os
from pathlib import Path

import uvicorn
from fastapi.responses import JSONResponse

from auto_promotion.api import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, default=8783)
    args = parser.parse_args()
    os.environ["AUTO_PROMOTION_LIVE_WRITES"] = "0"
    for key in list(os.environ):
        if key.startswith("AP_CLOUD_"):
            os.environ.pop(key)
    os.environ["AP_ACCOUNTS_DIR"] = str(Path(args.db).resolve().parent / "accounts")
    app = create_app(db_path=args.db, worker=False, report_worker=False, pooled_worker=False)

    @app.middleware("http")
    async def read_only(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path != "/api/session":
            return JSONResponse(
                {"error": "这是迁移副本的只读预览，不能启动或修改任务"}, status_code=403
            )
        return await call_next(request)

    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()

import { DurableObject } from "cloudflare:workers";
import { ControlKernel, ControlError, sha } from "./kernel.mjs";

export class WorkspaceCoordinator extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.kernel = new ControlKernel(ctx.storage);
  }
  async fetch(request) {
    try {
      const path = new URL(request.url).pathname;
      const body = request.method === "GET" ? {} : await request.json();
      let result;
      if (request.headers.get("X-Control-Role") === "operator") {
        if (path === "/v1/status") result = this.kernel.status();
        else if (path === "/v1/settings" && request.method === "PUT")
          result = this.kernel.configure(body);
        else if (path === "/v1/nodes" && request.method === "POST")
          result = this.kernel.registerNode(body);
        else if (path === "/v1/sync" && request.method === "POST")
          result = this.kernel.sync(body);
        else throw new ControlError("NotFound", 404);
      } else {
        const node = this.kernel.node(
          request.headers.get("X-Control-Node"),
          request.headers.get("X-Control-Node-Hash"),
        );
        if (path === "/v1/heartbeat" && request.method === "POST")
          result = { ready: true, node_id: node.id };
        else if (path === "/v1/claim" && request.method === "POST")
          result = {
            job: this.kernel.claim(node, body.pool, body.expected_id || null),
          };
        else if (path === "/v1/job" && request.method === "POST") {
          this.kernel.sweep();
          const job = this.kernel.one(
            "SELECT id,state,account_id,pool FROM jobs WHERE id=?",
            body.id,
          );
          if (
            job &&
            (!node.accounts.includes(job.account_id) ||
              !node.pools.includes(job.pool))
          )
            throw new ControlError("JobNotAssigned", 403);
          result = { job: job || null };
        } else if (path === "/v1/renew" && request.method === "POST")
          result = this.kernel.renew(node, body.id, body.lease_token);
        else if (path === "/v1/authorize" && request.method === "POST")
          result = this.kernel.authorize(
            node,
            body.id,
            body.lease_token,
            body.attempt_id,
          );
        else if (path === "/v1/complete" && request.method === "POST")
          result = this.kernel.complete(node, body.id, body.lease_token, body);
        else throw new ControlError("NotFound", 404);
      }
      if ((await this.ctx.storage.getAlarm()) === null)
        await this.ctx.storage.setAlarm(Date.now() + 60000);
      return Response.json(result, {
        headers: { "Cache-Control": "no-store" },
      });
    } catch (e) {
      return Response.json(
        { error: e instanceof ControlError ? e.message : "ControlPlaneError" },
        { status: e.status || 500, headers: { "Cache-Control": "no-store" } },
      );
    }
  }
  async alarm() {
    this.kernel.sweep();
    if (
      this.kernel.one(
        "SELECT id FROM jobs WHERE state IN ('pending','leased') LIMIT 1",
      )
    )
      await this.ctx.storage.setAlarm(Date.now() + 60000);
  }
}
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET")
      return Response.json({
        status: "ok",
        service: "auto-promotion-control",
        schema: 1,
      });
    if (!env.OPERATOR_TOKEN)
      return Response.json({ error: "ServiceNotConfigured" }, { status: 503 });
    const token =
      request.headers.get("Authorization")?.replace(/^Bearer /, "") || "";
    const headers = new Headers({ "Content-Type": "application/json" });
    if (token && (await sha(token)) === (await sha(env.OPERATOR_TOKEN)))
      headers.set("X-Control-Role", "operator");
    else {
      const node = request.headers.get("X-Node-ID");
      if (!node || !token)
        return Response.json({ error: "Unauthorized" }, { status: 401 });
      headers.set("X-Control-Role", "node");
      headers.set("X-Control-Node", node);
      headers.set("X-Control-Node-Hash", await sha(token));
    }
    const length = Number(request.headers.get("Content-Length") || 0);
    if (length > 4_000_000)
      return Response.json({ error: "SnapshotTooLarge" }, { status: 413 });
    const body = request.method === "GET" ? undefined : await request.text();
    if (body && body.length > 4_000_000)
      return Response.json({ error: "SnapshotTooLarge" }, { status: 413 });
    const stub = env.CONTROL.get(
      env.CONTROL.idFromName(env.WORKSPACE_ID || "auto-promotion"),
    );
    return stub.fetch(
      new Request("https://internal" + url.pathname, {
        method: request.method,
        headers,
        body,
      }),
    );
  },
};

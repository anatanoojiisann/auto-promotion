import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { build } from "../../control-plane/node_modules/esbuild/lib/main.js";
import {
  Miniflare,
  Log,
  LogLevel,
  convertV4MiniflareOptions,
} from "../../control-plane/node_modules/miniflare/dist/src/index.js";

test("production executor routes serialize readers, persist budget, preserve saved IDs and verify without IDs uses no RPC", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const source = (
    await readFile(new URL("../worker.mjs", import.meta.url), "utf8")
  ).replace(
    'input.request_id = requestId(request.headers?.get("X-AP-Request-ID"));',
    'input.request_id = requestId(request.headers?.get("X-AP-Request-ID")); if(op === "synthetic_fixture_receipt"){await this.ctx.storage.put("receipt:send:one",input.receipt);return Response.json({seeded:true});}',
  );
  const adapter = `
export const LIBRARY_VERSION='synthetic-only';
export const safeError=()=>({reason:'SyntheticFailure'});
export const loginToken=async()=>({});
export function createClient(){ const c={
  connect:async()=>{},destroy:async()=>{},session:{save:()=>''},
  invoke:async()=>{await new Promise(r=>setTimeout(r,20));return {};},
  getMe:async()=>{await c.invoke({className:'users.GetUsers'});return {id:'42'};}
};return c;}
export const readOperation=(client,op,input,read)=>read.execute(client,op,input,async()=>({status:'healthy',identity_verified:true}));
export const submitOperation=async()=>{throw new Error('NoSyntheticWrites');};`;
  const bundle = await build({
    stdin: { contents: source, resolveDir: root, sourcefile: "worker.mjs" },
    bundle: true,
    write: false,
    format: "esm",
    platform: "browser",
    external: ["cloudflare:workers"],
    plugins: [
      {
        name: "synthetic-read-client",
        setup(plugin) {
          plugin.onResolve({ filter: /telegram-adapter\.mjs$/ }, () => ({
            path: "adapter",
            namespace: "synthetic",
          }));
          plugin.onResolve({ filter: /qr-login-controller\.mjs$/ }, () => ({
            path: "login",
            namespace: "synthetic",
          }));
          plugin.onResolve({ filter: /^teleproto$/ }, () => ({
            path: "api",
            namespace: "synthetic",
          }));
          plugin.onLoad({ filter: /.*/, namespace: "synthetic" }, (args) => ({
            contents:
              args.path === "adapter"
                ? adapter
                : args.path === "api"
                  ? "export const Api={};"
                  : "export class QrLoginController{constructor(){this.active=false;}async view(){return {status:'not_logged_in'};}}",
          }));
        },
      },
    ],
  });
  const mf = new Miniflare(
    convertV4MiniflareOptions({
      name: "synthetic-read-budget",
      modules: true,
      script: bundle.outputFiles[0].text,
      compatibilityDate: "2025-09-01",
      cf: false,
      durableObjects: {
        TG: { className: "TelegramExecutor", useSQLite: true },
      },
      bindings: {
        TG_ACCOUNT_ID: "a",
        TG_EXPECTED_ID: "42",
        TG_API_ID: "123",
        TG_API_HASH: "synthetic",
        TG_SESSION_KEK: "a".repeat(64),
        TG_READ_POLICY_JSON: JSON.stringify({ requests_per_window: 2 }),
      },
      log: new Log(LogLevel.ERROR),
    }),
  );
  const request = async (body) =>
    mf.dispatchFetch("https://local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: "a", ...body }),
    });
  try {
    const concurrent = await Promise.all([
      request({ operation: "health" }),
      request({ operation: "health" }),
    ]);
    assert.deepEqual(concurrent.map((r) => r.status).sort(), [200, 409]);
    const busy = await concurrent.find((r) => r.status === 409).json();
    assert.equal(busy.status, "waiting_account_lock");
    const second = await (await request({ operation: "health" })).json();
    assert.equal(second.identity_verified, true);
    const exhausted = await (await request({ operation: "health" })).json();
    assert.equal(exhausted.reason, "TelegramReadBudgetExceeded");
    assert.equal(exhausted.cooldown_source, "local_read_budget");
    const noIds = await (
      await request({ operation: "verify", message_ids: [] })
    ).json();
    assert.equal(noIds.reason, "DurableMessageIdsRequired");
    const receipt = {
      account_id: "a",
      job_version: "v",
      attempt_id: "once",
      status: "unknown",
      message_ids: [77],
    };
    await request({ operation: "synthetic_fixture_receipt", receipt });
    const restored = await (
      await request({
        operation: "execute",
        job_id: "send:one",
        version: "v",
        attempt_id: "once",
        lease_token: "l",
      })
    ).json();
    assert.deepEqual(restored.message_ids, [77]);
    assert.equal(restored.from_durable_receipt, true);
    assert.equal(restored.status, "unknown");
  } finally {
    await mf.dispose();
  }
});

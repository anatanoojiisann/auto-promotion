import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from "../../control-plane/node_modules/miniflare/dist/src/index.js";

test("the QR controller creates a flow using real Worker timers and SQLite storage", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const path = (name) => fileURLToPath(new URL("../" + name, import.meta.url));
  const probe = `
    import { DurableObject } from 'cloudflare:workers';
    import { QrLoginController } from './qr-login-controller.mjs';
    export class Probe extends DurableObject {
      constructor(ctx, env) {
        super(ctx, env);
        this.login = new QrLoginController({
          store: ctx.storage, env: { TG_EXPECTED_ID:'synthetic-only' },
          createClient: () => ({ connect:async()=>{}, destroy:async()=>{}, getMe:async()=>({id:'synthetic-only'}), session:{save:()=> 'synthetic-only'} }),
          seal:async value=>({data:value}), unseal:async value=>value.data,
          waitUntil:p=>ctx.waitUntil(p), diagnosticSink:()=>{}
        });
      }
      async fetch(request) {
        const result = new URL(request.url).pathname === '/begin' ? await this.login.begin() : await this.login.view();
        return Response.json({status:result.status,flow_id:result.flow_id,generation:result.generation,has_qr:!!result.qr_uri});
      }
    }
    export default {fetch(request,env){return env.PROBE.get(env.PROBE.idFromName('synthetic')).fetch(request)}};
  `;
  const mf = new Miniflare(convertV4MiniflareOptions({
    name: "qr-native-runtime-test", modulesRoot: root,
    modules: [
      {type:"ESModule",path:path("native-runtime-probe.mjs"),contents:probe},
      {type:"ESModule",path:path("qr-login-controller.mjs"),contents:await readFile(path("qr-login-controller.mjs"),"utf8")},
      {type:"ESModule",path:path("qr-login-diagnostics.mjs"),contents:await readFile(path("qr-login-diagnostics.mjs"),"utf8")},
      {type:"ESModule",path:path("telegram-adapter.mjs"),contents:`export const safeError=e=>({reason:e.message||'SyntheticError'});export const loginToken=async()=>({status:'waiting_device_confirmation',expires_at:new Date(Date.now()+30000).toISOString(),qr_uri:'tg://login?token=synthetic_only_not_a_telegram_token'});`},
      {type:"ESModule",path:path("qr-login-flow.mjs"),contents:`export const loginError=code=>new Error(code);export function keepQrLoginAlive(){const done=Promise.resolve();done.cancel=async()=>{};return done;}`},
    ],
    compatibilityDate:"2025-09-01",
    durableObjects:{PROBE:{className:"Probe",useSQLite:true}}, cf:false,
    log:new Log(LogLevel.ERROR),
  }));
  try {
    const response=await mf.dispatchFetch("http://local/begin");
    assert.equal(response.status,200,await response.clone().text());
    const first=await response.json();
    assert.equal(first.status,"waiting_device_confirmation");
    assert.equal(first.generation,1);
    assert.equal(first.has_qr,true);
    const next=await (await mf.dispatchFetch("http://local/status")).json();
    assert.equal(next.flow_id,first.flow_id);
    assert.equal(next.generation,first.generation);
  } finally {await mf.dispose();}
});

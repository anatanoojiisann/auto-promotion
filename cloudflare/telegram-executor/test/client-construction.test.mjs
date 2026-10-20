import assert from "node:assert/strict";
import test from "node:test";
import {
  createClient,
  safeError,
  loginToken,
  readOperation,
} from "../telegram-adapter.mjs";
import { Api } from "teleproto";
test("real packaged Telegram client constructs without network and disables automatic retries", async () => {
  const client = createClient("", {
    TG_API_ID: 12345,
    TG_API_HASH: "synthetic-only",
  });
  assert.equal(client._requestRetries, 1);
  assert.equal(client._autoReconnect, false);
  assert.equal(client.floodSleepThreshold, 0);
  assert.equal(client._log.logLevel, "none");
  await client.destroy();
});
test("safe diagnostics never return raw runtime errors", () => {
  assert.deepEqual(
    safeError(new TypeError("Synthetic secret value not supported")),
    { reason: "CloudflareRuntimeCapabilityMissing" },
  );
});

test("Telegram cooldown and risk signals are retained for both executor and queue guards", () => {
  const flood = safeError({ errorMessage: "FLOOD_WAIT_180", seconds: 180 });
  assert.equal(flood.rate_limited, true);
  assert.equal(flood.retry_after_seconds, 180);
  const risk = safeError({ errorMessage: "PEER_FLOOD" });
  assert.equal(risk.risk_warning, true);
  assert.equal(risk.retry_after_seconds, 86400);
});

test("raw RPC options use the third parameter and never become a data center id", async () => {
  const calls = [];
  const client = {
    invoke: async (request, dcId, options) => {
      assert.equal(dcId, undefined);
      assert.equal(options.maxRetryCount, 0);
      calls.push(request.className);
      if (request instanceof Api.auth.ExportLoginToken)
        return new Api.auth.LoginToken({
          expires: 1800000000,
          token: Buffer.from("synthetic-login-token"),
        });
      return { chats: [] };
    },
  };
  const r = await loginToken(client, {
    TG_API_ID: 12345,
    TG_API_HASH: "synthetic-only",
  });
  assert.equal(r.status, "waiting_device_confirmation");
  assert.equal(r.expires_at, "2027-01-15T08:00:00.000Z");
  await readOperation(client, "discover", { query: "synthetic" });
  assert.equal(calls.length, 2);
});

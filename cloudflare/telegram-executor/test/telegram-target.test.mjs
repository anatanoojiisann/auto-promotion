import assert from "node:assert/strict";
import test from "node:test";
import {
  normalizeTelegramTarget,
  resolveSendPeer,
} from "../telegram-target.mjs";

test("canonical private supergroup and basic-group targets; reject users and ambiguous URLs", () => {
  assert.equal(
    normalizeTelegramTarget("https://web.telegram.org/a/#-1002123962275"),
    "-1002123962275",
  );
  assert.equal(normalizeTelegramTarget("-4929779361"), "-4929779361");
  assert.equal(normalizeTelegramTarget("@Synthetic"), "@synthetic");
  for (const t of [
    "123",
    "-0",
    "-1000000000000",
    "https://evil.test/#-1002123962275",
    "@ab",
    "https://t.me/+invite",
  ])
    assert.throws(() => normalizeTelegramTarget(t));
});
test("resolve private channel from authenticated session, require access hash and exact peer", async () => {
  const entity = {
    className: "Channel",
    id: 2123962275n,
    accessHash: 42n,
    megagroup: true,
  };
  const c = {
    iterDialogs: async function* () {
      yield { entity };
    },
    getEntity: async () => entity,
  };
  assert.equal(await resolveSendPeer(c, "-1002123962275"), entity);
  await assert.rejects(
    resolveSendPeer(
      {
        ...c,
        iterDialogs: async function* () {
          yield { entity: { ...entity, id: 3n } };
        },
      },
      "-1002123962275",
    ),
    /TelegramPeerUnavailable/,
  );
  await assert.rejects(
    resolveSendPeer(
      { ...c, iterDialogs: async function* () {} },
      "-1002123962275",
    ),
    /TelegramPeerUnavailable/,
  );
  await assert.rejects(
    resolveSendPeer(
      {
        ...c,
        iterDialogs: async function* () {
          yield { entity: { ...entity, accessHash: null } };
        },
      },
      "-1002123962275",
    ),
    /TelegramAccessHashRequired/,
  );
});
test("public and basic group resolution rejects replaced usernames, users and departed groups", async () => {
  const channel = {
    className: "Channel",
    id: 123n,
    accessHash: 42n,
    megagroup: true,
    username: "synthetic",
  };
  assert.equal(
    await resolveSendPeer({ getEntity: async () => channel }, "@synthetic"),
    channel,
  );
  for (const patch of [
    { username: "other" },
    { left: true },
    { className: "User" },
    { megagroup: false },
  ])
    await assert.rejects(
      resolveSendPeer(
        { getEntity: async () => ({ ...channel, ...patch }) },
        "@synthetic",
      ),
    );
  const chat = { className: "Chat", id: 4321n };
  assert.equal(
    await resolveSendPeer(
      {
        iterDialogs: async function* () {
          yield { entity: chat };
        },
      },
      "-4321",
    ),
    chat,
  );
});

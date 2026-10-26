import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { readFile, writeFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
const root = fileURLToPath(new URL("..", import.meta.url));
const executor = root + "/cloudflare/telegram-executor";
const require = createRequire(executor + "/package.json");
const blob = "65cb5866853f6cbaf773d11c2ac32eb53a640460";
const args = process.argv.slice(2);
if (args.length && (args.length !== 2 || args[0] !== "--baseline-file")) {
  console.error(
    "Usage: node scripts/telegram_cloud_read_compare.mjs [--baseline-file PATH]",
  );
  process.exit(2);
}
let baselineRaw;
try {
  baselineRaw = args.length
    ? await readFile(args[1], "utf8")
    : execFileSync("git", ["show", blob], {
        cwd: root,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      });
} catch {
  console.error(
    `Pinned baseline unavailable. Supply --baseline-file PATH containing source with Git blob ${blob}; a fresh clone may not contain this local uncommitted object.`,
  );
  process.exit(2);
}
const actual = execFileSync("git", ["hash-object", "--stdin"], {
  cwd: root,
  encoding: "utf8",
  input: baselineRaw,
}).trim();
if (actual !== blob) {
  console.error(
    `Baseline blob mismatch: expected ${blob}, got ${actual}. Comparison stopped.`,
  );
  process.exit(2);
}
let baselineSource = baselineRaw.replace(
  '"./telegram-target.mjs"',
  JSON.stringify(pathToFileURL(executor + "/telegram-target.mjs").href),
);
for (const module of [
  "teleproto",
  "teleproto/sessions",
  "teleproto/extensions",
])
  baselineSource = baselineSource.replace(
    JSON.stringify(module),
    JSON.stringify(pathToFileURL(require.resolve(module)).href),
  );
const temporary = await mkdtemp(join(tmpdir(), "ap-cloud-read-compare-"));
const baselinePath = join(temporary, "baseline.mjs");
await writeFile(baselinePath, baselineSource);
const baseline = await import(pathToFileURL(baselinePath));
await rm(temporary, { recursive: true, force: true });
const current = await import(pathToFileURL(executor + "/telegram-adapter.mjs"));
const { TelegramReadPolicy } = await import(
  pathToFileURL(executor + "/telegram-read-policy.mjs")
);
function fixture() {
  let calls = 0;
  const map = new Map();
  let transaction = Promise.resolve();
  const store = {
    get: async (k) => structuredClone(map.get(k)),
    put: async (k, v) => map.set(k, structuredClone(v)),
    delete: async (k) => map.delete(k),
  };
  store.transaction = (fn) => {
    const result = transaction.then(() => fn(store));
    transaction = result.catch(() => {});
    return result;
  };
  const client = {
    invoke: async (request) => {
      calls++;
      return request.className === "channels.GetFullChannel"
        ? { fullChat: { about: "synthetic rules", participantsCount: 10 } }
        : {};
    },
    getEntity: async () => {
      await client.invoke({ className: "contacts.ResolveUsername" });
      return {
        className: "Channel",
        id: 123n,
        accessHash: 456n,
        username: "synthetic",
        title: "synthetic",
        megagroup: true,
      };
    },
    getMessages: async (entity, options) => {
      await client.invoke({
        className: options.filter ? "messages.Search" : "messages.GetHistory",
      });
      return options.filter
        ? []
        : [5, 4, 3, 2, 1].map((id) => ({
            id,
            message: "synthetic " + id,
            date: 1800000000,
            out: false,
          }));
    },
  };
  return { store, client, calls: () => calls };
}
const before = fixture(),
  after = fixture();
let last;
for (let i = 0; i < 2; i++) {
  await before.client.invoke({ className: "users.GetUsers" });
  await baseline.readOperation(before.client, "inspect", {
    target: "@synthetic",
  });
}
for (let i = 0; i < 2; i++) {
  const read = new TelegramReadPolicy(after.store, {
    accountId: "a",
    identity: "self",
    revision: 1,
    clock: () => 1800000000,
  });
  read.instrument(after.client);
  try {
    await after.client.invoke({ className: "users.GetUsers" });
    last = await current.readOperation(
      after.client,
      "inspect",
      { target: "@synthetic" },
      read,
    );
  } finally {
    read.restore(after.client);
  }
}
console.log(
  JSON.stringify(
    {
      baseline_source: args.length
        ? "verified baseline file"
        : "pinned local Git blob",
      baseline_blob: blob,
      fixture: "two_identical_inspects_five_messages",
      before_logical_invoke_attempts: before.calls(),
      after_logical_invoke_attempts: after.calls(),
      second_read_state: last.read_state,
      second_read_fresh_samples: last.read_fresh_samples,
      second_rpc_counts: last.rpc_counts,
      physical_network_rpc_count: null,
      retries: null,
    },
    null,
    2,
  ),
);

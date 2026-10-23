function notReserved(t) {
  return !t.startsWith("-100") || /^-100[1-9][0-9]*$/.test(t);
}
export function normalizeTelegramTarget(value) {
  if (typeof value !== "string") throw new Error("InvalidTelegramTarget");
  let target = value.trim();
  const url = /^https:\/\/web\.telegram\.org\/[ak]\/#(-[1-9][0-9]{0,15})$/.exec(
    target,
  );
  if (url) target = url[1];
  if (/^@[a-zA-Z][a-zA-Z0-9_]{3,31}$/.test(target)) return target.toLowerCase();
  if (/^-[1-9][0-9]{0,15}$/.test(target) && notReserved(target)) return target;
  throw new Error("InvalidTelegramTarget");
}
export async function resolveSendPeer(client, value) {
  const target = normalizeTelegramTarget(value);
  let entity;
  if (target.startsWith("@")) {
    try {
      entity = await client.getEntity(target);
    } catch {
      throw new Error("TelegramPeerUnavailable");
    }
  } else {
    // StringSession does not persist access hashes. Resolve only through the
    // authenticated dialog listing; never invoke numeric getInputEntity's
    // accessHash=0 fallback and never accept a hash from the caller.
    if (typeof client.iterDialogs !== "function")
      throw new Error("TelegramPeerUnavailable");
    for await (const dialog of client.iterDialogs({ limit: 1000 })) {
      const e = dialog.entity;
      if (!e?.id) continue;
      const marked =
        e.className === "Channel"
          ? "-100" + String(e.id)
          : e.className === "Chat"
            ? "-" + String(e.id)
            : null;
      if (marked === target) {
        entity = e;
        break;
      }
    }
    if (!entity) throw new Error("TelegramPeerUnavailable");
  }
  const channel = entity?.className === "Channel" && entity.megagroup === true;
  const chat = entity?.className === "Chat";
  if (
    (!channel && !chat) ||
    entity.left ||
    entity.kicked ||
    entity.deactivated ||
    entity.migratedTo
  )
    throw new Error("TelegramGroupRequired");
  if (channel && (!entity.accessHash || String(entity.accessHash) === "0"))
    throw new Error("TelegramAccessHashRequired");
  const marked = channel ? "-100" + String(entity.id) : "-" + String(entity.id);
  if (
    target.startsWith("@")
      ? "@" + String(entity.username).toLowerCase() !== target
      : String(marked) !== target
  )
    throw new Error("TelegramPeerMismatch");
  return entity;
}

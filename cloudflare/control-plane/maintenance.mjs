export async function scheduleMaintenance(kernel, storage, now = Date.now()) {
  const current = await storage.getAlarm();
  const next = kernel.nextMaintenanceAt();
  // An existing earlier alarm is harmless (one read-only wakeup). Avoid moving
  // it later on every renewal/receipt and never arm an idle workspace.
  if (next === null) return;
  const at = Math.max(now + 1000, Math.ceil(next * 1000));
  if (current === null || at < current) await storage.setAlarm(at);
}

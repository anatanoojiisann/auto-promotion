import { signNativeGate } from "../control-plane/telegram-native-gate.mjs";

export async function authorizeNative(env, request) {
  if (!env.CONTROL_INTERNAL || !env.TG_QUEUE_BRIDGE)
    throw new Error("NativeGateNotConnected");
  const raw = JSON.stringify(request);
  const response = await env.CONTROL_INTERNAL.fetch(
    "https://internal/v1/executor/telegram/gate",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Executor-Proof": await signNativeGate(
          env.TG_QUEUE_BRIDGE,
          raw,
        ),
      },
      body: raw,
    },
  );
  if (!response.ok) throw new Error("NativeGateRejected");
  return await response.json();
}

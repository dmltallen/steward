import { config } from '../config';

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
};

let nextId = 0;
const makeId = () => `msg-${Date.now()}-${nextId++}`;

/**
 * Steward daemon client. Today it is a stub: the health check hits the real
 * /health route and the chat call returns a canned local reply so the full
 * composer flow is exercisable in the simulator. The T2 sprint replaces
 * sendChat with the real daemon endpoint; the *.tsx screens do not change.
 *
 * Daemon URL comes from config (env EXPO_PUBLIC_STEWARD_DAEMON_URL).
 */
export async function pingDaemon(): Promise<boolean> {
  try {
    const res = await fetch(`${config.daemonUrl}/health`, {
      signal: AbortSignal.timeout(3500),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export async function sendChat(
  history: ChatMessage[],
  text: string,
): Promise<ChatMessage> {
  // STUB: replaced by the daemon chat endpoint in T2. Kept local and honest
  // so no one mistakes it for a real model response.
  await new Promise((resolve) => setTimeout(resolve, 700));
  const replyCount = history.filter((m) => m.role === 'user').length;
  return {
    id: makeId(),
    role: 'assistant',
    text:
      replyCount > 1
        ? `Local stub reply (${replyCount} user messages so far). The chat screen is wired through the daemon client at ${config.daemonUrl}; T2 will connect a real model on your network.`
        : `Stub reply from the Steward daemon client at ${config.daemonUrl}. Nothing left this device: your assistant runs on hardware you own, with no subscription. T2 wires the real model here.`,
  };
}

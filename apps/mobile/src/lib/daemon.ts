import { config } from '../config';

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
};

/** Generate a client-side id only when the daemon reply lacks one. */
const fallbackId = () => `msg-${Date.now()}-${Math.floor(Math.random() * 1e4)}`;

/**
 * Steward daemon client. Talks to the real local daemon in
 * src/steward/server/daemon.py (fixed port from config). The daemon fronts the
 * Ollama backend, so chat replies here are real model output that never leaves
 * this device or network. The *.tsx screens do not depend on this file's guts.
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
  const res = await fetch(`${config.daemonUrl}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages: history, text }),
    signal: AbortSignal.timeout(120000),
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const err = await res.json();
      if (err && typeof err.error === 'string') detail = err.error;
    } catch {
      // keep the status code detail
    }
    throw new Error(`steward daemon error: ${detail}`);
  }
  const data = (await res.json()) as { id?: string; role: string; text: string };
  return {
    id: typeof data.id === 'string' && data.id ? data.id : fallbackId(),
    role: data.role === 'user' ? 'user' : 'assistant',
    text: data.text,
  };
}

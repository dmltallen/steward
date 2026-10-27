/**
 * Session management: the paired token plus where the daemon lives.
 *
 * The token itself is stored through a SecureTokenStore (expo-secure-store
 * in the app, a plain file in the Node tests); host/port/device metadata
 * ride in the KV storage. Both are injected so this module stays pure and
 * unit-testable.
 *
 * Pure module: no React Native or Expo imports (Node-testable).
 */

import { KEYS } from './cache';
import type { StorageLike } from './cache';
import type { Session } from './types';

export type SecureTokenStore = {
  load(): Promise<string | null>;
  save(token: string): Promise<void>;
  clear(): Promise<void>;
};

export class SessionManager {
  constructor(
    private readonly storage: StorageLike,
    private readonly secure: SecureTokenStore,
  ) {}

  /** Rebuild the session from storage and the secure token, or null. */
  async restore(): Promise<Session | null> {
    const token = await this.secure.load();
    const meta = await this.storage.getItem(KEYS.sessionMeta);
    if (!token || !meta) return null;
    try {
      const parsed = JSON.parse(meta) as Partial<Session> & { host: string; port: number };
      if (typeof parsed.host !== 'string' || typeof parsed.port !== 'number') {
        return null;
      }
      return {
        host: parsed.host,
        port: parsed.port,
        token,
        device: parsed.device ?? '',
        expiresAt: parsed.expiresAt ?? '',
        pairedAt: parsed.pairedAt ?? '',
      };
    } catch {
      return null;
    }
  }

  /** Persist a session: token in secure storage, the rest in KV. */
  async set(session: Session): Promise<void> {
    await this.secure.save(session.token);
    await this.storage.setItem(
      KEYS.sessionMeta,
      JSON.stringify({
        host: session.host,
        port: session.port,
        device: session.device,
        expiresAt: session.expiresAt,
        pairedAt: session.pairedAt,
      }),
    );
  }

  /** Forget the session entirely (unpair). */
  async clear(): Promise<void> {
    await this.secure.clear();
    await this.storage.removeItem?.(KEYS.sessionMeta);
  }
}

/** A device label for pairing: platform plus a persistent random suffix so
 * each install has a stable, human-readable identity. */
export async function deviceLabel(storage: StorageLike, platform: string): Promise<string> {
  const existing = await storage.getItem(KEYS.deviceId);
  if (existing) return `${platform}-${existing}`;
  const id = Math.random().toString(36).slice(2, 8);
  await storage.setItem(KEYS.deviceId, id);
  return `${platform}-${id}`;
}

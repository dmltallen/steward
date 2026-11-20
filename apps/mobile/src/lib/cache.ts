/**
 * Offline conversation cache: AsyncStorage-backed (in the app) or anything
 * with a getItem/setItem shape (in the tests: a temp-file store). Keeps the
 * last 20 messages of each conversation plus a summaries index, so the last
 * conversation renders with zero network.
 *
 * Pure module: no React Native or Expo imports (Node-testable).
 */

import type { Conversation, ConversationSummary, SourceRef } from './types';

/** Minimal KV storage, shaped like AsyncStorage. */
export type StorageLike = {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem?(key: string): Promise<void>;
};

export const MAX_CACHED_MESSAGES = 20;

export const KEYS = {
  seenOnboarding: 'steward.seenOnboarding.v1',
  deviceId: 'steward.deviceId.v1',
  sessionMeta: 'steward.session.meta.v1',
  convIndex: 'steward.convIndex.v1',
  conv: (id: number | string) => `steward.conv.v1.${id}`,
} as const;

export class ConversationCache {
  constructor(private readonly storage: StorageLike) {}

  /** Save a conversation, trimming to the last 20 messages; refresh index. */
  async saveConversation(conv: Conversation): Promise<void> {
    const trimmed: Conversation = {
      ...conv,
      messages: conv.messages.slice(-MAX_CACHED_MESSAGES),
    };
    await this.storage.setItem(KEYS.conv(conv.id), JSON.stringify(trimmed));
    const index = await this.list();
    const existing = index.find((s) => s.id === conv.id);
    const summary: ConversationSummary = {
      id: conv.id,
      title: conv.title || 'New conversation',
      created_at: conv.created_at,
      message_count: trimmed.messages.length,
    };
    const next: ConversationSummary[] = existing
      ? index.map((s) => (s.id === conv.id ? summary : s))
      : [summary, ...index];
    await this.storage.setItem(KEYS.convIndex, JSON.stringify(next));
  }

  /** Conversation summaries from cache, newest first, or [] when empty. */
  async list(): Promise<ConversationSummary[]> {
    const raw = await this.storage.getItem(KEYS.convIndex);
    if (!raw) return [];
    try {
      const arr = JSON.parse(raw) as ConversationSummary[];
      return Array.isArray(arr) ? arr : [];
    } catch {
      return [];
    }
  }

  /** The most recently saved conversation that has cached messages, or null. */
  async latest(): Promise<Conversation | null> {
    const order = await this.list();
    for (const summary of order) {
      const conv = await this.get(summary.id);
      if (conv && conv.messages.length > 0) return conv;
    }
    return null;
  }

  async get(id: number): Promise<Conversation | null> {
    const raw = await this.storage.getItem(KEYS.conv(id));
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw) as Conversation;
      if (typeof parsed.id !== 'number') return null;
      parsed.messages = Array.isArray(parsed.messages) ? parsed.messages : [];
      return parsed;
    } catch {
      return null;
    }
  }

  async remove(id: number): Promise<void> {
    if (this.storage.removeItem) await this.storage.removeItem(KEYS.conv(id));
    const index = await this.list();
    const next = index.filter((s) => s.id !== id);
    if (next.length === 0) {
      if (this.storage.removeItem) await this.storage.removeItem(KEYS.convIndex);
    } else {
      await this.storage.setItem(KEYS.convIndex, JSON.stringify(next));
    }
  }

  async clear(): Promise<void> {
    const index = await this.list();
    if (this.storage.removeItem) {
      for (const s of index) await this.storage.removeItem(KEYS.conv(s.id));
      await this.storage.removeItem(KEYS.convIndex);
    }
  }
}

/** Reconcile a live server conversation into cache form. */
export function toCachedConversation(
  id: number,
  title: string,
  createdAt: string,
  messages: { role: string; content: string; sources?: SourceRef[] }[],
): Conversation {
  return {
    id,
    title: title || 'New conversation',
    created_at: createdAt,
    messages: messages.map((m, i) => ({
      id: dateNowPrefixed(i),
      role: m.role,
      content: m.content,
      created_at: createdAt,
      model: null,
      sources: m.sources,
    })),
  };
}

function dateNowPrefixed(offset: number): number {
  // Stable per-call ids for local messages: timestamp + small offset keeps
  // order while remaining unique within a single cache write.
  return Date.now() * 1000 + offset;
}

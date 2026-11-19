/**
 * High-level Steward client: the surface the screens use, and the object
 * under test in the pairing / SSE / offline-cache unit tests.
 *
 * Composes the pure primitives (net.ts, cache.ts, session.ts). Holds no
 * React Native or Expo imports, so a fake daemon in a Node test exercises
 * the exact same code path as the app.
 */

import { ConversationCache, toCachedConversation } from './cache';
import type { StorageLike } from './cache';
import {
  checkUpdateAuthed,
  createConversationAuthed,
  decideApprovalAuthed,
  deleteConversationAuthed,
  fetchHealth,
  getConversationAuthed,
  getLicenseAuthed,
  listConversationsAuthed,
  postPair,
  StewardError,
  streamChatAuthed,
} from './net';
import type { ChatWireMessage } from './net';
import { daemonUrl } from './pairing';
import { SessionManager } from './session';
import type {
  Conversation,
  ConversationSummary,
  HealthInfo,
  LicenseReport,
  Session,
  ToolStreamEvent,
  UpdateCheckReport,
} from './types';

export type PairPersistOptions = {
  host: string;
  port: number;
  code: string;
  device: string;
  sessions: SessionManager;
};

export class StewardClient {
  private constructor(
    readonly baseUrl: string,
    private readonly token: string,
    private readonly cache: ConversationCache,
  ) {}

  /** Bind a client to a session. Pass cache for offline recovery. */
  static fromSession(session: Session, cache: ConversationCache): StewardClient {
    return new StewardClient(daemonUrl(session.host, session.port), session.token, cache);
  }

  /** True when a session token was supplied (paired device). */
  get paired(): boolean {
    return Boolean(this.token);
  }

  /**
   * The pairing flow, end to end: exchange the 6-digit code for a signed
   * session token and persist it. "code -> token -> session". Contact with
   * the daemon (authed chat) is verified by streaming with the returned
   * token afterwards; a wrong code raises StewardError (401).
   */
  static async pairAndPersist(opts: PairPersistOptions): Promise<Session> {
    const baseUrl = daemonUrl(opts.host, opts.port);
    const pair = await postPair(baseUrl, opts.code, opts.device);
    const session: Session = {
      host: opts.host,
      port: opts.port,
      token: pair.token,
      device: opts.device,
      expiresAt: pair.expires_at,
      pairedAt: new Date().toISOString(),
    };
    await opts.sessions.set(session);
    return session;
  }

  /** GET /health. Needs no token; reachability check. */
  health(): Promise<HealthInfo> {
    return fetchHealth(this.baseUrl);
  }

  /**
   * Conversation list from the daemon, falling back to the offline cache
   * when the network is down (auth failures are rethrown, never masked).
   */
  async listConversations(): Promise<{
    summaries: ConversationSummary[];
    fromCache: boolean;
  }> {
    try {
      const summaries = await listConversationsAuthed(this.baseUrl, this.token);
      return { summaries, fromCache: false };
    } catch (err) {
      if (err instanceof StewardError && err.kind === 'http' && err.status === 401) {
        throw err;
      }
      return { summaries: await this.cache.list(), fromCache: true };
    }
  }

  /** A conversation plus its messages, cached copy when offline. */
  async openConversation(id: number): Promise<{
    conversation: Conversation;
    fromCache: boolean;
  }> {
    try {
      const conversation = await getConversationAuthed(this.baseUrl, this.token, id);
      await this.cache.saveConversation(conversation);
      return { conversation, fromCache: false };
    } catch (err) {
      if (err instanceof StewardError && err.kind === 'http' && err.status === 401) {
        throw err;
      }
      const cached = await this.cache.get(id);
      if (cached) return { conversation: cached, fromCache: true };
      throw err;
    }
  }

  /** Recover the most recently cached conversation with zero network. */
  latestCached(): Promise<Conversation | null> {
    return this.cache.latest();
  }

  cachedSummaries(): Promise<ConversationSummary[]> {
    return this.cache.list();
  }

  async createConversation(title?: string): Promise<{
    id: number;
    title: string;
    created_at: string;
  }> {
    return createConversationAuthed(this.baseUrl, this.token, title);
  }

  async deleteConversation(id: number): Promise<void> {
    await deleteConversationAuthed(this.baseUrl, this.token, id);
    await this.cache.remove(id);
  }

  /** Write (or refresh) the cached copy of a conversation, last 20 messages. */
  cacheConversation(conversation: Conversation): Promise<void> {
    return this.cache.saveConversation(conversation);
  }

  /**
   * Build the cache-friendly Conversation from locally held messages. Used
   * after a stream completes so the offline copy always has the newest pair.
   */
  buildCachedConversation(args: {
    id: number;
    title: string;
    createdAt: string;
    messages: { role: string; content: string }[];
  }): Conversation {
    return toCachedConversation(args.id, args.title, args.createdAt, args.messages);
  }

  /**
   * Stream one assistant reply through the daemon. Deltas flow to onDelta as
   * they arrive. Resolves with the full reply text. Abort via signal to stop
   * generation; an aborted stream resolves with what had streamed so far.
   * Tool frames flow to onEvent; they never contaminate the resolved text.
   */
  streamChat(opts: {
    history: ChatWireMessage[];
    text: string;
    conversationId: number | null;
    signal?: AbortSignal;
    onDelta: (delta: string) => void;
    onDone?: () => void;
    onError?: (message: string) => void;
    onEvent?: (event: ToolStreamEvent) => void;
  }): Promise<string> {
    return streamChatAuthed({
      baseUrl: this.baseUrl,
      token: this.token,
      ...opts,
    });
  }

  /**
   * Approve or deny one pending tool approval. The id is the stream id from
   * the approval_required frame the client rendered.
   */
  decideApproval(id: string | number, decision: 'approve' | 'deny'): Promise<unknown> {
    return decideApprovalAuthed(this.baseUrl, this.token, id, decision);
  }

  /** License report from GET /v1/license (free vs perpetual). */
  license(): Promise<LicenseReport> {
    return getLicenseAuthed(this.baseUrl, this.token);
  }

  /** Schema-v2 update report from GET /v1/update/check (bearer-gated). */
  checkUpdate(): Promise<UpdateCheckReport> {
    return checkUpdateAuthed(this.baseUrl, this.token);
  }
}

/** Convenience typed export so tests and screens share one error class. */
export { StewardError };
export type { StorageLike };

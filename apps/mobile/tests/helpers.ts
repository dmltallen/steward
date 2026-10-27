/**
 * Shared test helpers: a fake Steward daemon over node:http plus in-memory /
 * file-backed storage and secure-token stores.
 *
 * The fake daemon mirrors src/steward/server/daemon.py (S2) for the routes
 * the mobile client uses, so the TypeScript client under test talks to the
 * same contract as a real daemon: pairing code -> token, Bearer-gated SSE
 * chat, conversation CRUD, and a license route that 404s (as daemon 0.2.0
 * does).
 */

import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import type { StorageLike } from '../src/lib/cache';
import type { SecureTokenStore } from '../src/lib/session';

export type FakeDaemonInit = {
  pairCode?: string; // expected pairing code
  token?: string; // the single accepted session token
  deltas?: string[]; // SSE delta pieces for /v1/chat
  streamDelayMs?: number; // delay between deltas (and after [DONE])
  conversations?: Record<string, unknown>[]; // GET /v1/conversations body
  keepStreamOpenMs?: number; // hold the stream open after [DONE] if set
};

type EffectiveInit = {
  pairCode: string;
  token: string;
  deltas: string[];
  streamDelayMs: number;
  conversations: Record<string, unknown>[];
  keepStreamOpenMs: number | null;
};

const PAIR_CODE = '123456';
const TOKEN = 'tok-real';
const DELTAS = ['Hel', 'lo ', 'from ', 'Steward'];

export class FakeDaemon {
  private server: Server;
  port = 0;
  host = '127.0.0.1';
  pairCalls = 0;
  chatCalls = 0;
  failPairCalls = 0;
  private init: EffectiveInit;

  private constructor(server: Server, init: EffectiveInit) {
    this.server = server;
    this.init = init;
    this.server.on('request', (req, res) => void this.route(req, res));
  }

  get baseUrl(): string {
    return `http://${this.host}:${this.port}`;
  }

  static start(
    init: FakeDaemonInit = {},
  ): Promise<FakeDaemon> {
    const defaults: EffectiveInit = {
      pairCode: PAIR_CODE,
      token: TOKEN,
      deltas: DELTAS,
      streamDelayMs: 10,
      conversations: [],
      keepStreamOpenMs: null,
    };
    const effective: EffectiveInit = {
      ...defaults,
      ...init,
      conversations: init.conversations ?? [],
    };
    return new Promise((resolve, reject) => {
      const server = createServer();
      const daemon = new FakeDaemon(server, effective);
      server.once('error', reject);
      server.listen(0, '127.0.0.1', () => {
        const addr = server.address() as AddressInfo;
        daemon.port = addr.port;
        resolve(daemon);
      });
    });
  }

  stop(): Promise<void> {
    return new Promise((resolve) => {
      this.server.close(() => resolve());
    });
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((r) => setTimeout(r, ms));
  }

  private json(res: import('node:http').ServerResponse, status: number, body: unknown): void {
    const raw = JSON.stringify(body);
    res.writeHead(status, {
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(raw),
    });
    res.end(raw);
  }

  private unauthorized(res: import('node:http').ServerResponse): void {
    this.json(res, 401, { error: 'unauthorized' });
  }

  private async route(
    req: import('node:http').IncomingMessage,
    res: import('node:http').ServerResponse,
  ): Promise<void> {
    const url = new URL(req.url ?? '/', this.baseUrl);
    const path = url.pathname;
    const method = (req.method ?? 'GET').toUpperCase();

    // Authorization check for gated routes.
    const authed =
      (req.headers.authorization ?? '') === `Bearer ${this.init.token}`;

    if (path === '/health' && method === 'GET') {
      this.json(res, 200, {
        status: 'ok',
        backend: 'fake-runtime',
        model: 'qwen3:8b',
        model_resident: true,
        version: '0.2.0',
        paired: 1,
      });
      return;
    }

    if (path === '/v1/pair' && method === 'POST') {
      this.pairCalls += 1;
      let body = {};
      for await (const chunk of req) body = { ...body, ...safeJson(chunk) };
      const attempt = (body as { code?: string }).code ?? '';
      if (attempt === this.init.pairCode) {
        this.json(res, 200, {
          token: this.init.token,
          expires_at: '2026-11-25T00:00:00Z',
          device: (body as { device?: string }).device ?? 'device',
          paired: true,
        });
      } else {
        this.failPairCalls += 1;
        this.json(res, 401, { error: 'invalid pairing code' });
      }
      return;
    }

    if (path === '/v1/chat' && method === 'POST') {
      if (!authed) {
        this.unauthorized(res);
        return;
      }
      this.chatCalls += 1;
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'close',
      });
      const closed = new Promise<void>((r) => {
        req.on('close', () => r());
      });
      let stopped = false;
      closed.then(() => {
        stopped = true;
      });
      for (const d of this.init.deltas) {
        if (stopped) return;
        res.write(`data: ${JSON.stringify({ delta: d })}\n\n`);
        await Promise.race([this.sleep(this.init.streamDelayMs), closed]);
      }
      if (!stopped) {
        res.write('data: [DONE]\n\n');
        if (this.init.keepStreamOpenMs) await this.sleep(this.init.keepStreamOpenMs);
      }
      res.end();
      return;
    }

    if (path === '/v1/conversations' && method === 'GET') {
      if (!authed) {
        this.unauthorized(res);
        return;
      }
      this.json(res, 200, { conversations: this.init.conversations });
      return;
    }

    if (path === '/v1/conversations' && method === 'POST') {
      if (!authed) {
        this.unauthorized(res);
        return;
      }
      this.json(res, 201, { id: 9001, title: 'New conversation', created_at: '2026-10-26T00:00:00Z' });
      return;
    }

    const convMatch = /^\/v1\/conversations\/(\d+)$/.exec(path);
    if (convMatch && method === 'GET') {
      if (!authed) {
        this.unauthorized(res);
        return;
      }
      this.json(res, 200, {
        id: Number(convMatch[1]),
        title: 'Cached conversation',
        created_at: '2026-10-26T00:00:00Z',
        messages: [
          { id: 1, role: 'user', content: 'hi', created_at: '2026-10-26T00:00:00Z', model: null },
          { id: 2, role: 'assistant', content: 'hello from fake', created_at: '2026-10-26T00:00:00Z', model: 'qwen3:8b' },
        ],
      });
      return;
    }

    if (convMatch && method === 'DELETE') {
      if (!authed) {
        this.unauthorized(res);
        return;
      }
      this.json(res, 200, { ok: true, deleted: Number(convMatch[1]) });
      return;
    }

    if (path === '/v1/license') {
      // The real daemon 0.2.0 does not serve this route.
      this.json(res, 404, { error: 'not found' });
      return;
    }

    this.json(res, 404, { error: 'not found' });
  }
}

function safeJson(chunk: unknown): Record<string, unknown> {
  try {
    const text = chunk ? Buffer.from(chunk as Buffer).toString('utf-8') : '';
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** In-memory KV storage (AsyncStorage shape). */
export class MapStorage implements StorageLike {
  constructor(private map: Map<string, string> = new Map()) {}
  async getItem(key: string): Promise<string | null> {
    return this.map.get(key) ?? null;
  }
  async setItem(key: string, value: string): Promise<void> {
    this.map.set(key, value);
  }
  async removeItem(key: string): Promise<void> {
    this.map.delete(key);
  }
  /** Number of stored keys (test introspection). */
  count(): number {
    return this.map.size;
  }
}

/** In-memory secure token store. */
export class MapSecureStore implements SecureTokenStore {
  private value: string | null = null;
  constructor(initial: string | null = null) {
    this.value = initial;
  }
  async load(): Promise<string | null> {
    return this.value;
  }
  async save(token: string): Promise<void> {
    this.value = token;
  }
  async clear(): Promise<void> {
    this.value = null;
  }
}

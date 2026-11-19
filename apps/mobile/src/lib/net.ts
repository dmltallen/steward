/**
 * Plain HTTP client for the Steward daemon API (S2).
 *
 * Pure module: only the global fetch, so it runs identically in the app
 * (Expo SDK 57 global fetch = expo/fetch, which streams) and in the Node
 * unit-test harness against a fake daemon.
 *
 * Endpoints (see src/steward/server/daemon.py):
 *   GET  /health                          open
 *   POST /v1/pair {code, device}          open, rate-limited -> token
 *   POST /v1/chat {messages, text, ...}   Bearer -> SSE deltas + [DONE]
 *   GET  /v1/conversations                Bearer -> summaries
 *   POST /v1/conversations {title?}       Bearer -> created conversation
 *   GET  /v1/conversations/{id}           Bearer -> conversation + messages
 *   DELETE /v1/conversations/{id}         Bearer
 *   GET  /v1/license                      Bearer (not served by daemon 0.2.0)
 */

import { streamSse } from './sse';
import type {
  Conversation,
  ConversationSummary,
  HealthInfo,
  LicenseReport,
  ToolStreamEvent,
  UpdateCheckReport,
} from './types';

export type NetErrorKind = 'http' | 'network' | 'aborted';

/** Typed error so the UI and tests can distinguish a 401, a dead host, and
 * a user-aborted stream without string matching. */
export class StewardError extends Error {
  readonly kind: NetErrorKind;
  readonly status: number | null;
  constructor(kind: NetErrorKind, message: string, status: number | null = null) {
    super(message);
    this.name = 'StewardError';
    this.kind = kind;
    this.status = status;
  }
}

export type AuthHeaders = Record<string, string>;

export async function jsonOrThrow(res: Response, label: string): Promise<unknown> {
  if (res.ok) {
    try {
      return await res.json();
    } catch {
      return null;
    }
  }
  let detail = `${res.status}`;
  try {
    const err = (await res.json()) as { error?: unknown };
    if (typeof err.error === 'string') detail = err.error;
  } catch {
    // keep the status detail
  }
  throw new StewardError('http', `${label} failed (${detail})`, res.status);
}

function toNetworkError(label: string, cause: unknown): StewardError {
  if (cause instanceof StewardError) return cause;
  if (cause instanceof Error && cause.name === 'AbortError') {
    return new StewardError('aborted', `${label} aborted`);
  }
  return new StewardError('network', `${label} network error: ${errMessage(cause)}`);
}

function errMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

/** GET /health. Does not need a token. */
export async function fetchHealth(baseUrl: string, timeoutMs = 3000): Promise<HealthInfo> {
  const res = await fetchT(baseUrl, '/health', { timeoutMs });
  const data = (await jsonOrThrow(res, 'health')) as Record<string, unknown> | null;
  const ok = (data && data.status === 'ok') || (data && data.ok === true) || false;
  return {
    status: data?.status != null ? String(data.status) : 'unknown',
    backend: data?.backend != null ? String(data.backend) : 'unknown',
    ollamaUrl: data?.ollama_url != null ? String(data.ollama_url) : null,
    model: data?.model != null ? String(data.model) : 'unknown',
    modelResident: data?.model_resident === true,
    version: data?.version != null ? String(data.version) : 'unknown',
    paired: typeof data?.paired === 'number' ? data.paired : 0,
    ok,
  };
}

export type PairResponse = {
  token: string;
  expires_at: string;
  device: string;
  paired: boolean;
};

/** POST /v1/pair. Throws StewardError on a bad code (401) or lockout (429). */
export async function postPair(
  baseUrl: string,
  code: string,
  device: string,
  timeoutMs = 8000,
): Promise<PairResponse> {
  const res = await fetchT(baseUrl, '/v1/pair', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, device }),
    timeoutMs,
  });
  const data = (await jsonOrThrow(res, 'pair')) as PairResponse;
  return data;
}

export type ChatWireMessage = { role: string; text?: string; content?: string };

export type StreamChatParams = {
  baseUrl: string;
  token: string;
  history: ChatWireMessage[];
  /** Fresh user turn; the daemon appends this last. */
  text: string;
  conversationId: number | null;
  signal?: AbortSignal;
  onDelta: (delta: string) => void;
  onDone?: () => void;
  onError?: (message: string) => void;
  /** Tool frames (tool_call, approval_required, tool_result) as they land. */
  onEvent?: (event: ToolStreamEvent) => void;
};

/**
 * POST /v1/chat and consume the SSE stream. Deltas stream through onDelta as
 * they arrive; resolves with the full assistant text after [DONE] (or EOF).
 * "Stop generation" is an abort on the signal: an aborted stream resolves
 * with whatever had streamed so far and the caller sees the aborted state it
 * already tracked. A 401 raises StewardError before any delta is delivered.
 */
export async function streamChatAuthed(p: StreamChatParams): Promise<string> {
  const body = {
    messages: p.history,
    text: p.text,
    ...(p.conversationId != null ? { conversation_id: p.conversationId } : {}),
  };
  let res: Response;
  try {
    res = await fetch(`${baseUrl(p.baseUrl)}/v1/chat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...authHeaders(p.token),
      },
      body: JSON.stringify(body),
      signal: p.signal,
    });
  } catch (err) {
    throw toNetworkError('chat', err);
  }
  if (!res.ok) {
    throw await jsonOrThrow(res, 'chat');
  }
  if (!res.body) {
    throw new StewardError('network', 'chat response has no body stream');
  }
  const parts: string[] = [];
  try {
    await streamSse(
      res,
      {
        onDelta: (d) => {
          parts.push(d);
          p.onDelta(d);
        },
        onDone: p.onDone,
        onError: p.onError,
        onEvent: (evt) => {
          p.onEvent?.(evt);
        },
      },
      p.signal,
    );
  } catch (err) {
    throw toNetworkError('chat', err);
  }
  return parts.join('');
}

/**
 * POST /v1/approvals/{id} with a decision. The stream id from an
 * approval_required frame is the approval id on the daemon, so this is how
 * the phone approves or denies a tool step. Resolves on a 200; throws
 * StewardError (404 unknown, 409 already decided, 401 bad token).
 */
export async function decideApprovalAuthed(
  baseUrl: string,
  token: string,
  id: string | number,
  decision: 'approve' | 'deny',
  timeoutMs = 8000,
): Promise<unknown> {
  const res = await fetchT(baseUrl, `/v1/approvals/${encodeURIComponent(String(id))}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ decision }),
    timeoutMs,
  });
  return jsonOrThrow(res, 'approval decision');
}

export async function listConversationsAuthed(
  baseUrl: string,
  token: string,
  timeoutMs = 8000,
): Promise<ConversationSummary[]> {
  const res = await fetchT(baseUrl, '/v1/conversations', {
    headers: authHeaders(token),
    timeoutMs,
  });
  const data = (await jsonOrThrow(res, 'conversations')) as {
    conversations?: unknown;
  } | null;
  const list = data?.conversations ?? [];
  return Array.isArray(list) ? (list as ConversationSummary[]) : [];
}

export async function getConversationAuthed(
  baseUrl: string,
  token: string,
  id: number,
  timeoutMs = 8000,
): Promise<Conversation> {
  const res = await fetchT(baseUrl, `/v1/conversations/${id}`, {
    headers: authHeaders(token),
    timeoutMs,
  });
  return (await jsonOrThrow(res, 'conversation')) as Conversation;
}

export async function createConversationAuthed(
  baseUrl: string,
  token: string,
  title?: string,
  timeoutMs = 8000,
): Promise<{ id: number; title: string; created_at: string }> {
  const res = await fetchT(baseUrl, '/v1/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ title: title ?? '' }),
    timeoutMs,
  });
  return (await jsonOrThrow(res, 'create conversation')) as {
    id: number;
    title: string;
    created_at: string;
  };
}

export async function deleteConversationAuthed(
  baseUrl: string,
  token: string,
  id: number,
  timeoutMs = 8000,
): Promise<void> {
  const res = await fetchT(baseUrl, `/v1/conversations/${id}`, {
    method: 'DELETE',
    headers: authHeaders(token),
    timeoutMs,
  });
  await jsonOrThrow(res, 'delete conversation');
}

/**
 * GET /v1/license. The daemon does not serve this route yet (version 0.2.0
 * route set, S2), so a 404 maps to the honest "not-served" state instead of
 * an error the UI would misreport.
 */
export async function getLicenseAuthed(
  baseUrl: string,
  token: string,
  timeoutMs = 8000,
): Promise<LicenseReport> {
  let res: Response;
  try {
    res = await fetchT(baseUrl, '/v1/license', {
      headers: authHeaders(token),
      timeoutMs,
    });
  } catch (err) {
    return {
      state: 'error',
      reason: errMessage(err),
    };
  }
  if (res.status === 404) {
    return {
      state: 'not-served',
      reason: 'daemon does not serve /v1/license yet (daemon 0.2.0 route set)',
    };
  }
  if (!res.ok) {
    return { state: 'error', reason: `license endpoint returned ${res.status}` };
  }
  try {
    const data = (await res.json()) as { tier?: unknown; label?: unknown };
    const tier = data.tier === 'perpetual' ? 'perpetual' : 'free';
    return {
      state: data.tier === 'perpetual' ? 'perpetual' : 'free-eval',
      info: {
        tier,
        label: typeof data.label === 'string' ? data.label : '',
      },
    };
  } catch {
    return { state: 'error', reason: 'license endpoint returned unreadable data' };
  }
}

/**
 * GET /v1/update/check. Bearer-gated like every data route. Returns the
 * schema-v2 report: catalog + app halves plus the verified manifest, so the
 * mobile app can find its own APK payload among the per-platform entries.
 */
export async function checkUpdateAuthed(
  baseUrl: string,
  token: string,
  timeoutMs = 8000,
): Promise<UpdateCheckReport> {
  const res = await fetchT(baseUrl, '/v1/update/check', {
    headers: authHeaders(token),
    timeoutMs,
  });
  return (await jsonOrThrow(res, 'update check')) as UpdateCheckReport;
}

export type { UpdateCheckReport };

/**
 * Fetch a file (e.g. the APK payload). Returns the raw bytes on a 200;
 * throws StewardError otherwise. No auth header added here: the app payload
 * URL is vendor-served (relative to the manifest source) and is verified by
 * sha256 against the signed manifest before anything is installed.
 */
export async function fetchBytes(
  url: string,
  timeoutMs = 12000,
): Promise<Uint8Array> {
  let res: Response;
  try {
    res = await fetch(url, {
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    throw toNetworkError('download', err);
  }
  if (!res.ok) {
    throw new StewardError('http', `download failed (${res.status})`, res.status);
  }
  const buf = await res.arrayBuffer();
  return new Uint8Array(buf);
}

// -- fetch helpers -----------------------------------------------------------

function authHeaders(token: string): AuthHeaders {
  return { Authorization: `Bearer ${token}` };
}

function baseUrl(u: string): string {
  return u.replace(/\/+$/, '');
}

type FetchTOptions = {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
  timeoutMs: number;
};

async function fetchT(baseUrlInput: string, path: string, o: FetchTOptions): Promise<Response> {
  try {
    return await fetch(`${baseUrl(baseUrlInput)}${path}`, {
      method: o.method ?? 'GET',
      headers: o.headers,
      body: o.body,
      signal: AbortSignal.timeout(o.timeoutMs),
    });
  } catch (err) {
    throw toNetworkError(path, err);
  }
}

export { errMessage };

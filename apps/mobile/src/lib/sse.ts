/**
 * Minimal Server-Sent Events reader over a fetch Response body.
 *
 * Expo SDK 57 installs expo/fetch as the global fetch, which streams a
 * genuine ReadableStream body (body.getReader()). We use that when present
 * and fall back to a full-body read otherwise, so the same parser serves
 * Hermes (streaming), Expo Go (streaming), and environments where the body
 * is not a real stream.
 *
 * The daemon (src/steward/server/daemon.py) emits:
 *   data: {"delta": "Hello "}
 *
 *   data: {"delta": "from "}
 *   ...
 *   data: {"event":"tool_call","id":"...","tool":"...","args":{...},"risk":"..."}
 *   data: {"event":"approval_required","id":"...","tool":"...","args":{...},"risk":"...","expires_at":"..."}
 *   data: {"event":"tool_result","id":"...","ok":true,"summary":"...","ms":123}
 *   data: {"event":"sources","sources":[{"n":1,"path":"...","start_line":3,"end_line":19,"snippet":"..."}]}
 *   data: [DONE]
 *
 * The sources frame arrives after the reply deltas and before [DONE]; it is
 * the retrieval-grounded source list behind the answer (F4 wire).
 *
 * Pure module: no React Native or Expo imports (Node-testable).
 */

import type { SourceRef, ToolStreamEvent } from './types';

export type SseMessage =
  | { kind: 'delta'; delta: string }
  | { kind: 'sources'; sources: SourceRef[] }
  | { kind: 'done' }
  | { kind: 'error'; message: string }
  | { kind: 'tool'; event: ToolStreamEvent }
  | { kind: 'unknown'; raw: string };

/** Parse one SSE event (all raw lines between blank separators) into a
 * message. Handles `data:` lines joined with newlines, the daemon's JSON
 * delta/error frames, and the literal `data: [DONE]` terminator. */
export function parseSseEvent(raw: string): SseMessage {
  const normalized = raw.replace(/\r\n/g, '\n');
  const data: string[] = [];
  for (const line of normalized.split('\n')) {
    if (line.startsWith('data:')) {
      data.push(line.slice(5).replace(/^ /, ''));
    }
  }
  const payload = data.join('\n').trim();
  if (payload === '') {
    return { kind: 'unknown', raw };
  }
  if (payload === '[DONE]') {
    return { kind: 'done' };
  }
  try {
    const parsed = JSON.parse(payload) as Record<string, unknown>;
    if (typeof parsed.delta === 'string') {
      return { kind: 'delta', delta: parsed.delta };
    }
    if (typeof parsed.error === 'string') {
      return { kind: 'error', message: parsed.error };
    }
    if (parsed.event === 'sources') {
      return { kind: 'sources', sources: parseSources(parsed.sources) };
    }
    const tool = parseToolEvent(parsed);
    if (tool) {
      return { kind: 'tool', event: tool };
    }
    return { kind: 'unknown', raw: payload };
  } catch {
    return { kind: 'unknown', raw: payload };
  }
}

function asString(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function asRecord(v: unknown): Record<string, unknown> {
  return v !== null && typeof v === 'object' ? (v as Record<string, unknown>) : {};
}

function asNumberOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** Tolerantly validate an SSE sources payload into SourceRef rows. Rows that
 * are not objects are dropped; a malformed payload yields an empty list,
 * never a crash. */
function parseSources(value: unknown): SourceRef[] {
  if (!Array.isArray(value)) return [];
  const out: SourceRef[] = [];
  for (const row of value) {
    if (row === null || typeof row !== 'object') continue;
    const r = row as Record<string, unknown>;
    out.push({
      n: typeof r.n === 'number' ? r.n : 0,
      path: asString(r.path),
      start_line: asNumberOrNull(r.start_line),
      end_line: asNumberOrNull(r.end_line),
      snippet: asString(r.snippet),
    });
  }
  return out;
}

/** Map a JSON frame carrying an event name onto the tool stream event. */
function parseToolEvent(parsed: Record<string, unknown>): ToolStreamEvent | null {
  const event = asString(parsed.event);
  const id = asString(parsed.id);
  if (event === 'tool_call') {
    return {
      type: 'tool_call',
      id,
      tool: asString(parsed.tool),
      args: asRecord(parsed.args),
      risk: asString(parsed.risk),
    };
  }
  if (event === 'approval_required') {
    return {
      type: 'approval_required',
      id,
      tool: asString(parsed.tool),
      args: asRecord(parsed.args),
      risk: asString(parsed.risk),
      expires_at: asString(parsed.expires_at),
    };
  }
  if (event === 'tool_result') {
    const ms = typeof parsed.ms === 'number' ? parsed.ms : null;
    return {
      type: 'tool_result',
      id,
      ok: parsed.ok === true,
      summary: asString(parsed.summary),
      ms,
    };
  }
  return null;
}

export type SseConsumer = {
  onDelta: (delta: string) => void;
  onDone?: () => void;
  onError?: (message: string) => void;
  /** Fired for tool_call / approval_required / tool_result frames. */
  onEvent?: (event: ToolStreamEvent) => void;
  /** Fired for the final sources payload of a grounded turn. */
  onSources?: (sources: SourceRef[]) => void;
};

type NextChunk = () => Promise<Uint8Array | ArrayBuffer | null>;

function streamReader(response: Response): NextChunk {
  const body = response.body;
  if (body && typeof body.getReader === 'function') {
    const reader = body.getReader();
    return async () => {
      const { done, value } = await reader.read();
      return done ? null : (value ?? new Uint8Array(0));
    };
  }
  // No streamable body (non-Expo environments). Read the whole response once;
  // deltas arrive together, correctness is preserved.
  let done = false;
  return async () => {
    if (done) return null;
    done = true;
    return response.arrayBuffer();
  };
}

/**
 * Consume an SSE response, invoking the consumer as frames land. Resolves
 * after [DONE] or when the stream ends. A signal that is already aborted
 * stops the loop on the next read, which is how "stop generation" works.
 *
 * Throws on missing body only. Network and abort errors are surfaced to the
 * caller (abort is mapped to a clean stop, not an error).
 */
export async function streamSse(
  response: Response,
  consumer: SseConsumer,
  signal?: AbortSignal,
): Promise<void> {
  const reader = streamReader(response);
  const decoder = new TextDecoder();
  let buffer = '';
  let aborted = false;
  while (true) {
    if (signal?.aborted) {
      aborted = true;
      break;
    }
    const chunk = await reader();
    if (chunk === null) break;
    if (chunk instanceof Uint8Array) {
      if (chunk.length > 0) buffer += decoder.decode(chunk, { stream: true });
    } else if (chunk.byteLength > 0) {
      buffer += decoder.decode(new Uint8Array(chunk), { stream: true });
    }
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const msg = parseSseEvent(frame);
      if (msg.kind === 'delta') consumer.onDelta(msg.delta);
      else if (msg.kind === 'done') {
        consumer.onDone?.();
        return;
      } else if (msg.kind === 'error') {
        consumer.onError?.(msg.message);
      } else if (msg.kind === 'tool') {
        consumer.onEvent?.(msg.event);
      } else if (msg.kind === 'sources') {
        consumer.onSources?.(msg.sources);
      }
    }
  }
  if (aborted) return; // caller treats an aborted stream as a clean stop
  consumer.onDone?.(); // EOF without [DONE]: end of stream
}

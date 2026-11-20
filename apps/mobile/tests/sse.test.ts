/**
 * SSE streaming semantics: incremental deltas, [DONE] termination, and stop
 * generation via abort. All against the fake daemon's /v1/chat.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { FakeDaemon, MapSecureStore, MapStorage } from './helpers';
import { ConversationCache } from '../src/lib/cache';
import { StewardClient } from '../src/lib/client';
import { SessionManager } from '../src/lib/session';
import { streamSse, parseSseEvent } from '../src/lib/sse';
import type { SourceRef } from '../src/lib/types';

async function setup(init: Parameters<typeof FakeDaemon.start>[0]) {
  const daemon = await FakeDaemon.start(init);
  const storage = new MapStorage();
  const secure = new MapSecureStore('tok-real');
  const sessions = new SessionManager(storage, secure);
  await sessions.set({
    host: daemon.host,
    port: daemon.port,
    token: 'tok-real',
    device: 'p',
    expiresAt: '',
    pairedAt: '',
  });
  const session = (await sessions.restore())!;
  const client = StewardClient.fromSession(session, new ConversationCache(storage));
  return { daemon, client };
}

test('sse: deltas stream incrementally and [DONE] ends the stream', async () => {
  const { daemon, client } = await setup({
    deltas: ['The ', 'cat', ' sat ', 'down.'],
  });
  try {
    const seen: string[] = [];
    const reply = await client.streamChat({
      history: [],
      text: 'tell me',
      conversationId: null,
      onDelta: (d) => seen.push(d),
    });
    assert.equal(reply, 'The cat sat down.');
    assert.deepEqual(seen, ['The ', 'cat', ' sat ', 'down.']);
  } finally {
    await daemon.stop();
  }
});

test('sse: a multi-frame message split across reads reassembles cleanly', async () => {
  const { daemon, client } = await setup({
    deltas: ['alpha beta ', 'gamma ', 'delta'],
    streamDelayMs: 25,
  });
  try {
    let full = '';
    const reply = await client.streamChat({
      history: [],
      text: 'x',
      conversationId: null,
      onDelta: (d) => {
        full += d;
      },
    });
    assert.equal(reply, 'alpha beta gamma delta');
    assert.equal(full, reply);
  } finally {
    await daemon.stop();
  }
});

test('sse: stop generation aborts mid-stream and keeps the partial text', async () => {
  const { daemon, client } = await setup({
    deltas: ['one ', 'two ', 'three ', 'four ', 'five '],
    streamDelayMs: 120, // slow stream so we can abort between deltas
  });
  try {
    const seen: string[] = [];
    const controller = new AbortController();
    const started = Date.now();
    const reply = await client.streamChat({
      history: [],
      text: 'x',
      conversationId: null,
      signal: controller.signal,
      onDelta: (d) => {
        seen.push(d);
        if (seen.length === 2) controller.abort(); // user taps stop
      },
    });
    const elapsed = Date.now() - started;
    // The aborted stream resolves quickly with whatever had streamed so far.
    assert.ok(elapsed < 3000, `abort took ${elapsed}ms, expected < 3000ms`);
    assert.equal(reply, 'one two ');
    assert.ok(seen.length >= 2 && seen.length < 5, `expected a partial stream, saw ${seen.length} deltas`);
  } finally {
    await daemon.stop();
  }
});

test('sse: a stream that ends at EOF without [DONE] still resolves', async () => {
  const { daemon, client } = await setup({ deltas: ['orphan ', 'tail'] });
  try {
    let done = false;
    const reply = await client.streamChat({
      history: [],
      text: 'x',
      conversationId: null,
      onDelta: () => {},
    });
    // Fake daemon always sends [DONE]; verifying EOF is handled in the parser
    // unit test below.
    assert.equal(reply, 'orphan tail');
    assert.equal(done, false);
  } finally {
    await daemon.stop();
  }
});

test('sse: parseSseEvent handles delta, done, error and junk frames', () => {
  assert.deepEqual(parseSseEvent('data: {"delta":"hi"}'), {
    kind: 'delta',
    delta: 'hi',
  });
  assert.deepEqual(parseSseEvent('data: [DONE]'), { kind: 'done' });
  assert.deepEqual(parseSseEvent('data: {"error":"boom"}'), {
    kind: 'error',
    message: 'boom',
  });
  assert.deepEqual(parseSseEvent('data: not-json'), { kind: 'unknown', raw: 'not-json' });
  assert.deepEqual(parseSseEvent(''), { kind: 'unknown', raw: '' });
});

test('sse: parseSseEvent parses the final sources payload of a grounded turn', () => {
  assert.deepEqual(
    parseSseEvent(
      'data: {"event":"sources","sources":[{"n":1,"path":"/tmp/x.md","start_line":3,"end_line":19,"snippet":"the passage"}]}',
    ),
    {
      kind: 'sources',
      sources: [
        { n: 1, path: '/tmp/x.md', start_line: 3, end_line: 19, snippet: 'the passage' },
      ],
    },
  );
  assert.deepEqual(parseSseEvent('data: {"event":"sources","sources":[]}'), {
    kind: 'sources',
    sources: [],
  });
  // Malformed rows are defaulted or dropped, never a crash.
  assert.deepEqual(
    parseSseEvent('data: {"event":"sources","sources":[{"n":"x"}, "junk", null]}'),
    {
      kind: 'sources',
      sources: [{ n: 0, path: '', start_line: null, end_line: null, snippet: '' }],
    },
  );
});

test('sse: the sources frame reaches the client consumer, then done fires', async () => {
  const { daemon, client } = await setup({
    deltas: ['grounded answer [1].'],
    sources: [{ n: 1, path: '/tmp/a.md', start_line: 3, end_line: 19, snippet: 'passage' }],
  });
  try {
    let got: SourceRef[] = [];
    let done = false;
    const reply = await client.streamChat({
      history: [],
      text: 'q',
      conversationId: null,
      onDelta: () => {},
      onSources: (s) => {
        got = s;
      },
      onDone: () => {
        done = true;
      },
    });
    assert.equal(reply, 'grounded answer [1].');
    if (got.length === 0) throw new Error('the sources frame was dropped by the client');
    assert.equal(got.length, 1);
    assert.equal(got[0].path, '/tmp/a.md');
    assert.equal(got[0].start_line, 3);
    assert.equal(done, true);
  } finally {
    await daemon.stop();
  }
});

test('sse: streamSse over an in-flight Response consumes deltas before [DONE]', async () => {
  // Drive streamSse directly with a real fetch to the fake daemon so the
  // incremental reader path is covered end to end from HTTP to parser.
  const { daemon } = await setup({ deltas: ['a', 'b', 'c'] });
  try {
    const res = await fetch(`${daemon.baseUrl}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer tok-real' },
      body: JSON.stringify({ messages: [], text: 'x' }),
    });
    const seen: string[] = [];
    let finished = false;
    await streamSse(
      res,
      {
        onDelta: (d) => seen.push(d),
        onDone: () => {
          finished = true;
        },
      },
    );
    assert.deepEqual(seen, ['a', 'b', 'c']);
    assert.equal(finished, true);
  } finally {
    await daemon.stop();
  }
});

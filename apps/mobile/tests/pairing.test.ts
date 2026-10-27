/**
 * Pairing + authed chat against a fake daemon: the "code -> token -> authed
 * chat" chain the sprint requires, plus the auth-failure matrix.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { FakeDaemon, MapSecureStore, MapStorage } from './helpers';
import { ConversationCache } from '../src/lib/cache';
import { StewardClient, StewardError } from '../src/lib/client';
import { streamChatAuthed } from '../src/lib/net';
import { SessionManager } from '../src/lib/session';

test('pairing: 6-digit code is traded for a token that authorizes chat', async () => {
  const daemon = await FakeDaemon.start({
    pairCode: '135790',
    token: 'tok-authorized',
    deltas: ['Sure', ', ', 'here is your reply.'],
  });
  const storage = new MapStorage();
  const secure = new MapSecureStore();
  const sessions = new SessionManager(storage, secure);
  const cache = new ConversationCache(storage);
  try {
    const session = await StewardClient.pairAndPersist({
      host: daemon.host,
      port: daemon.port,
      code: '135790',
      device: 'test-phone',
      sessions,
    });

    assert.equal(session.token, 'tok-authorized');
    assert.equal(session.host, '127.0.0.1');
    assert.equal(session.port, daemon.port);
    // The token lives in the secure store, never in plain KV.
    assert.equal(await secure.load(), 'tok-authorized');
    const meta = await storage.getItem('steward.session.meta.v1');
    assert.ok(meta, 'session metadata (host/port) is stored in KV');
    assert.ok(!meta!.includes('tok-authorized'), 'token must not be persisted in plain KV');
    // A later restore rebuilds the session with the same token.
    const restored = await sessions.restore();
    assert.ok(restored);
    assert.equal(restored?.token, 'tok-authorized');

    // Authed chat streams a real reply with that token.
    const client = StewardClient.fromSession(session, cache);
    const deltas: string[] = [];
    const reply = await client.streamChat({
      history: [],
      text: 'hello',
      conversationId: null,
      onDelta: (d) => deltas.push(d),
    });
    assert.equal(reply, 'Sure, here is your reply.');
    assert.deepEqual(deltas, ['Sure', ', ', 'here is your reply.']);
    assert.equal(daemon.chatCalls, 1);
  } finally {
    await daemon.stop();
  }
});

test('pairing: a wrong code is rejected and nothing is persisted', async () => {
  const daemon = await FakeDaemon.start({ pairCode: '123456' });
  const storage = new MapStorage();
  const secure = new MapSecureStore(null);
  const sessions = new SessionManager(storage, secure);
  try {
    await assert.rejects(
      () =>
        StewardClient.pairAndPersist({
          host: daemon.host,
          port: daemon.port,
          code: '000000',
          device: 'bad-phone',
          sessions,
        }),
      (err: unknown) =>
        err instanceof StewardError && err.kind === 'http' && err.status === 401,
    );
    assert.equal(await secure.load(), null);
    assert.equal(storage.count(), 0);
    assert.equal(await sessions.restore(), null);
  } finally {
    await daemon.stop();
  }
});

test('pairing: missing or bad bearer token is rejected before any delta', async () => {
  const daemon = await FakeDaemon.start({ token: 'tok-real' });
  try {
    await assert.rejects(
      () =>
        streamChatAuthed({
          baseUrl: daemon.baseUrl,
          token: 'forged-token',
          history: [],
          text: 'x',
          conversationId: null,
          onDelta: () => {},
        }),
      (err: unknown) =>
        err instanceof StewardError && err.kind === 'http' && err.status === 401,
    );
    await assert.rejects(
      () =>
        streamChatAuthed({
          baseUrl: daemon.baseUrl,
          token: '',
          history: [],
          text: 'x',
          conversationId: null,
          onDelta: () => {},
        }),
      (err: unknown) =>
        err instanceof StewardError && err.kind === 'http' && err.status === 401,
    );
    assert.equal(daemon.chatCalls, 0);
  } finally {
    await daemon.stop();
  }
});

test('license: daemon 0.2.0 does not serve /v1/license, client reports not-served', async () => {
  const daemon = await FakeDaemon.start({});
  const storage = new MapStorage();
  const secure = new MapSecureStore('tok-real');
  const sessions = new SessionManager(storage, secure);
  const cache = new ConversationCache(storage);
  try {
    // Seed a session whose token the fake daemon accepts.
    await sessions.set({
      host: daemon.host,
      port: daemon.port,
      token: 'tok-real',
      device: 'p',
      expiresAt: '',
      pairedAt: '',
    });
    const session = (await sessions.restore())!;
    const client = StewardClient.fromSession(session, cache);
    const report = await client.license();
    if (report.state !== 'not-served') {
      assert.fail(`expected not-served license report, got ${report.state}`);
    }
    assert.match(report.reason, /0\.2\.0/);
  } finally {
    await daemon.stop();
  }
});

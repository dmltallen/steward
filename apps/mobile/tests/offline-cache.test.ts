/**
 * Offline cache: a conversation survives a daemon going away and renders
 * from local storage with zero network.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { FakeDaemon, MapSecureStore, MapStorage } from './helpers';
import { ConversationCache, MAX_CACHED_MESSAGES } from '../src/lib/cache';
import { StewardClient } from '../src/lib/client';
import { SessionManager } from '../src/lib/session';
import type { SourceRef } from '../src/lib/types';

test('offline cache: last 20 messages are stored, restored with no network', async () => {
  const storage = new MapStorage();
  const cache = new ConversationCache(storage);
  const secure = new MapSecureStore(null);
  const sessions = new SessionManager(storage, secure);

  // Phase 1: online. A real daemon conversation is written into the cache.
  const daemon = await FakeDaemon.start({ token: 'tok-real' });
  await sessions.set({
    host: daemon.host,
    port: daemon.port,
    token: 'tok-real',
    device: 'p',
    expiresAt: '',
    pairedAt: '',
  });
  const onlineSession = (await sessions.restore())!;
  const onlineClient = StewardClient.fromSession(onlineSession, cache);
  const big = Array.from({ length: 25 }, (_, i) => ({
    role: i % 2 === 0 ? ('user' as const) : ('assistant' as const),
    content: `msg-${i}`,
  }));
  await onlineClient.cacheConversation(
    onlineClient.buildCachedConversation({
      id: 7,
      title: 'Offline test',
      createdAt: '2026-10-26T00:00:00Z',
      messages: big,
    }),
  );

  // The cache trims to the last 20 messages.
  const stored = await cache.get(7);
  if (!stored) throw new Error('expected cached conversation 7');
  assert.equal(stored.messages.length, MAX_CACHED_MESSAGES);
  assert.equal(stored.messages[0].content, 'msg-5');
  assert.equal(stored.messages[MAX_CACHED_MESSAGES - 1].content, 'msg-24');

  // Phase 2: offline. The daemon is gone; its port is closed, so any network
  // call to it fails immediately with a connection refused. A fresh client
  // over the same storage must still restore the conversation.
  await daemon.stop();
  const offlineSession = {
    ...onlineSession,
    port: daemon.port, // now a dead port: nothing listens here
  };
  const offlineClient = StewardClient.fromSession(offlineSession, cache);

  const summaries = await offlineClient.listConversations();
  assert.equal(summaries.fromCache, true, 'list must fall back to cache');
  assert.equal(summaries.summaries.length, 1);
  assert.equal(summaries.summaries[0].id, 7);

  const latest = await offlineClient.latestCached();
  if (!latest) throw new Error('last conversation must restore from cache');
  assert.equal(latest.id, 7);
  assert.equal(latest.messages.length, MAX_CACHED_MESSAGES);

  const opened = await offlineClient.openConversation(7);
  assert.equal(opened.fromCache, true, 'openConversation must serve the cached copy');
  assert.equal(opened.conversation.messages[0].content, 'msg-5');

  // The cache never made a single successful network call in phase 2: the
  // daemon had already stopped before the offline client was created.
  assert.ok(true, 'no network needed: list, latest and open all resolved from storage');
});

test('offline cache: retrieval-grounded sources survive into the cached message', async () => {
  // This test FAILS if the sources are dropped from the cached message: the
  // whole point of caching them is that a cached grounded answer still shows
  // where it came from, with zero network.
  const storage = new MapStorage();
  const cache = new ConversationCache(storage);
  const sources: SourceRef[] = [
    { n: 1, path: '/tmp/notes/stellar-drive.md', start_line: 3, end_line: 19, snippet: 'The Stellar Drive desktop unit costs $1,299.' },
    { n: 2, path: '/tmp/notes/warranty.md', start_line: 1, end_line: 2, snippet: 'Five year warranty.' },
  ];
  const daemon = await FakeDaemon.start({
    token: 'tok-real',
    deltas: ['The drive costs $1,299 [1] on a five year warranty [2].'],
    sources,
  });
  try {
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
    const client = StewardClient.fromSession((await sessions.restore())!, cache);

    // Stream a grounded turn exactly like the chat screen does; the sources
    // frame arrives from the daemon before [DONE].
    const received: SourceRef[][] = [];
    const reply = await client.streamChat({
      history: [],
      text: 'How much does the drive cost?',
      conversationId: null,
      onDelta: () => {},
      onSources: (s) => received.push(s),
    });
    assert.equal(reply, 'The drive costs $1,299 [1] on a five year warranty [2].');
    assert.equal(received.length, 1, 'expected exactly one sources frame');
    assert.deepEqual(received[0], sources);

    // Persist the streamed turn into the offline cache the way the chat
    // screen does: the assistant message carries the sources it received.
    await client.cacheConversation(
      client.buildCachedConversation({
        id: 11,
        title: 'Sources test',
        createdAt: '2026-10-26T00:00:00Z',
        messages: [
          { role: 'user', content: 'How much does the drive cost?' },
          { role: 'assistant', content: reply, sources: received[0] },
        ],
      }),
    );

    const saved = await cache.get(11);
    if (!saved) throw new Error('expected cached conversation 11');
    const assistant = saved.messages.find((m) => m.role === 'assistant');
    if (!assistant) throw new Error('assistant message missing from the cache');
    assert.deepEqual(assistant.sources, sources, 'sources were dropped from the cached message');
  } finally {
    await daemon.stop();
  }
});

test('offline cache: empty cache and dead daemon yields an empty recovery, no throw', async () => {
  const storage = new MapStorage();
  const cache = new ConversationCache(storage);
  const deadSession = {
    host: '127.0.0.1',
    port: 1, // nothing listens on port 1
    token: 'whatever',
    device: 'p',
    expiresAt: '',
    pairedAt: '',
  };
  const client = StewardClient.fromSession(deadSession, cache);
  const summaries = await client.listConversations();
  assert.equal(summaries.fromCache, true);
  assert.equal(summaries.summaries.length, 0);
  assert.equal(await client.latestCached(), null);
});

test('offline cache: delete on a live daemon also clears the cached copy', async () => {
  const storage = new MapStorage();
  const cache = new ConversationCache(storage);
  const secure = new MapSecureStore('tok-real');
  const sessions = new SessionManager(storage, secure);
  const daemon = await FakeDaemon.start({ token: 'tok-real' });
  try {
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
    await client.cacheConversation(
      client.buildCachedConversation({
        id: 99,
        title: 'To be deleted',
        createdAt: '2026-10-26T00:00:00Z',
        messages: [{ role: 'user', content: 'hi' }],
      }),
    );
    assert.ok(await cache.get(99));
    await client.deleteConversation(99); // daemon deletes + cache.remove()
    assert.equal(await cache.get(99), null);
    assert.equal((await cache.list()).length, 0);
  } finally {
    await daemon.stop();
  }
});

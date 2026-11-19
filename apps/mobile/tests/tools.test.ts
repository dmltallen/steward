/**
 * F8 tool handling on the phone: tool frames stream out of /v1/chat, steps
 * render inline and settle as done/failed, and one-tap Approve / Deny posts
 * to POST /v1/approvals/{id}. Every test here fails if the approval path is
 * removed: no onEvent wiring, no decideApproval, no step fold, or no frame
 * parsing means an assertion below breaks.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { FakeDaemon, MapSecureStore, MapStorage } from './helpers';
import { ConversationCache } from '../src/lib/cache';
import { StewardClient } from '../src/lib/client';
import { SessionManager } from '../src/lib/session';
import { parseSseEvent } from '../src/lib/sse';
import {
  applyToolEvent,
  approvalStatus,
  formatCountdown,
  summarizeToolArgs,
} from '../src/lib/tools';
import type { ToolStep, ToolStreamEvent } from '../src/lib/types';

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

test('tools: tool frames stream through onEvent and never leak into the reply text', async () => {
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  const { daemon, client } = await setup({
    deltas: ['Let me '],
    toolFrames: [
      {
        event: 'tool_call',
        id: 't1',
        tool: 'fs.write',
        args: { path: '/notes/demo.txt', content: 'hello' },
        risk: 'write',
      },
      {
        event: 'approval_required',
        id: 't1',
        tool: 'fs.write',
        args: { path: '/notes/demo.txt', content: 'hello' },
        risk: 'write',
        expires_at: expiresAt,
      },
      { event: 'tool_result', id: 't1', ok: true, summary: 'Wrote demo.txt (5 bytes)', ms: 180 },
    ],
  });
  try {
    const events: ToolStreamEvent[] = [];
    const reply = await client.streamChat({
      history: [],
      text: 'write a note',
      conversationId: null,
      onDelta: () => {},
      onEvent: (e) => events.push(e),
    });
    assert.equal(reply, 'Let me '); // tool frames must not be part of the text
    assert.equal(events.length, 3, 'expected the three tool frames to be surfaced');
    assert.deepEqual(events[0], {
      type: 'tool_call',
      id: 't1',
      tool: 'fs.write',
      args: { path: '/notes/demo.txt', content: 'hello' },
      risk: 'write',
    });
    assert.equal(events[1].type, 'approval_required');
    if (events[1].type === 'approval_required') {
      assert.equal(events[1].id, 't1');
      assert.equal(events[1].expires_at, expiresAt);
    }
    assert.deepEqual(events[2], {
      type: 'tool_result',
      id: 't1',
      ok: true,
      summary: 'Wrote demo.txt (5 bytes)',
      ms: 180,
    });
  } finally {
    await daemon.stop();
  }
});

test('tools: a turn with tool events caches its final text without the event frames', async () => {
  const { daemon, client } = await setup({
    deltas: ['Saved ', 'the note.'],
    toolFrames: [
      {
        event: 'tool_call',
        id: 'c1',
        tool: 'fs.write',
        args: { path: '/notes/demo.txt' },
        risk: 'write',
      },
      {
        event: 'approval_required',
        id: 'c1',
        tool: 'fs.write',
        args: { path: '/notes/demo.txt' },
        risk: 'write',
        expires_at: new Date(Date.now() + 60_000).toISOString(),
      },
      { event: 'tool_result', id: 'c1', ok: true, summary: 'Wrote demo.txt', ms: 90 },
    ],
  });
  try {
    const reply = await client.streamChat({
      history: [],
      text: 'please save it',
      conversationId: null,
      onDelta: () => {},
    });
    assert.equal(reply, 'Saved the note.');
    const cached = client.buildCachedConversation({
      id: 12,
      title: 'Tools turn',
      createdAt: new Date().toISOString(),
      messages: [
        { role: 'user', content: 'please save it' },
        { role: 'assistant', content: reply },
      ],
    });
    const raw = JSON.stringify(cached);
    assert.ok(raw.includes('Saved the note.'), 'cached text must be present');
    assert.equal(raw.includes('approval_required'), false, 'event frames must not be cached');
    assert.equal(raw.includes('tool_result'), false, 'event frames must not be cached');
  } finally {
    await daemon.stop();
  }
});

test('tools: approve and deny post the decision to POST /v1/approvals/{id}', async () => {
  const { daemon, client } = await setup({});
  try {
    await client.decideApproval('t1', 'approve');
    await client.decideApproval(7, 'deny');
    assert.deepEqual(daemon.approvalDecisions, [
      { id: 't1', decision: 'approve' },
      { id: '7', decision: 'deny' },
    ]);
  } finally {
    await daemon.stop();
  }
});

test('tools: steps fold running -> approval -> done, and deny settles as failed', () => {
  let steps: ToolStep[] = [];
  steps = applyToolEvent(steps, {
    type: 'tool_call',
    id: 'a',
    tool: 'shell.run',
    args: { command: 'ls -la' },
    risk: 'exec',
  });
  assert.equal(steps.length, 1);
  assert.equal(steps[0].state, 'running');

  steps = applyToolEvent(steps, {
    type: 'approval_required',
    id: 'a',
    tool: 'shell.run',
    args: { command: 'ls -la' },
    risk: 'exec',
    expires_at: '2026-12-31T00:00:00Z',
  });
  assert.equal(steps[0].state, 'approval');
  assert.equal(steps[0].expiresAt, '2026-12-31T00:00:00Z');

  steps = applyToolEvent(steps, {
    type: 'tool_result',
    id: 'a',
    ok: true,
    summary: 'Listed 3 entries',
    ms: 12,
  });
  assert.equal(steps[0].state, 'done');
  assert.equal(steps[0].summary, 'Listed 3 entries');
  assert.equal(steps[0].ms, 12);

  let denied: ToolStep[] = [];
  denied = applyToolEvent(denied, {
    type: 'approval_required',
    id: 'b',
    tool: 'fs.write',
    args: { path: '/p' },
    risk: 'write',
    expires_at: '2026-12-31T00:00:00Z',
  });
  assert.equal(denied[0].state, 'approval');
  denied = applyToolEvent(denied, {
    type: 'tool_result',
    id: 'b',
    ok: false,
    summary: 'Denied by approval. No changes were made.',
    ms: 0,
  });
  assert.equal(denied[0].state, 'failed');
  assert.equal(denied[0].summary, 'Denied by approval. No changes were made.');
});

test('tools: args summarize to one human line, never raw JSON', () => {
  const line = summarizeToolArgs('fs.write', {
    path: '/home/notes/x.txt',
    content: 'a'.repeat(80),
  });
  assert.equal(line.includes('{'), false, 'never raw JSON');
  assert.ok(line.includes('path=/home/notes/x.txt'));
  assert.ok(line.includes('content='));
  assert.ok(line.length < 140, 'stays one compact line');
  assert.equal(summarizeToolArgs('fs.read', {}), 'no arguments');
  assert.equal(summarizeToolArgs('fs.read', null), 'no arguments');
});

test('tools: countdown helpers show a live label and lapse to expired', () => {
  const in60 = new Date(Date.now() + 60_000).toISOString();
  const live = approvalStatus(in60, Date.now());
  assert.equal(live.expired, false);
  assert.match(live.label, /^expires in \d+:\d\d$/);
  assert.equal(formatCountdown(60_000), '1:00');
  assert.equal(formatCountdown(0), '0:00');

  const past = approvalStatus(new Date(Date.now() - 1000).toISOString(), Date.now());
  assert.equal(past.expired, true);
  assert.equal(past.label, 'expired, ask again');
});

test('tools: parseSseEvent recognizes the tool frames', () => {
  const call = parseSseEvent(
    'data: {"event":"tool_call","id":"x","tool":"fs.read","args":{"path":"/p"},"risk":"read"}',
  );
  assert.equal(call.kind, 'tool');
  if (call.kind === 'tool') assert.equal(call.event.type, 'tool_call');

  const approval = parseSseEvent(
    'data: {"event":"approval_required","id":"x","tool":"fs.read","args":{"path":"/p"},"risk":"read","expires_at":"2026-12-31T00:00:00Z"}',
  );
  assert.equal(approval.kind, 'tool');

  const result = parseSseEvent(
    'data: {"event":"tool_result","id":"x","ok":false,"summary":"Denied","ms":3}',
  );
  assert.equal(result.kind, 'tool');
  if (result.kind === 'tool' && result.event.type === 'tool_result') {
    assert.equal(result.event.ok, false);
    assert.equal(result.event.summary, 'Denied');
  }
});

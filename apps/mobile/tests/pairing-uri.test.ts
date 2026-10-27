/**
 * Pairing URI parsing and host normalization. The daemon (S2) prints its QR
 * payload without a code (steward://pair?host=&port=); the parser tolerates
 * an optional code for forward compatibility.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  COMMON_GATEWAYS,
  DEFAULT_DAEMON_PORT,
  daemonUrl,
  findDaemonAtGatewayCandidates,
  normalizeHost,
  parsePairUri,
} from '../src/lib/pairing';

test('pair-uri: S2 payload shape without code parses', () => {
  const parsed = parsePairUri('steward://pair?host=192.168.1.50&port=3877');
  assert.deepEqual(parsed, { host: '192.168.1.50', port: 3877, code: null });
});

test('pair-uri: optional code parameter is accepted and prefilled', () => {
  const parsed = parsePairUri('steward://pair?host=github.com&port=3877&code=482913');
  assert.deepEqual(parsed, { host: 'github.com', port: 3877, code: '482913' });
});

test('pair-uri: garbage, empty and wrong-scheme values return null', () => {
  assert.equal(parsePairUri(''), null);
  assert.equal(parsePairUri(null as unknown as string), null);
  assert.equal(parsePairUri('https://pair?host=1.2.3.4'), null);
  assert.equal(parsePairUri('steward://pair'), null);
  assert.equal(parsePairUri('steward://pair?host='), null);
  assert.equal(parsePairUri('steward://pair?host=1.2.3.4&port=bad'), null);
});

test('pair-uri: a missing port falls back to the daemon default', () => {
  const parsed = parsePairUri('steward://pair?host=10.0.0.9');
  assert.equal(parsed?.port, DEFAULT_DAEMON_PORT);
});

test('pair-uri: normalizeHost strips scheme, slashes and an inline port', () => {
  assert.equal(normalizeHost('  http://192.168.1.50/  '), '192.168.1.50');
  assert.equal(normalizeHost('192.168.1.50:3877'), '192.168.1.50');
  assert.equal(normalizeHost('steward-box.local'), 'steward-box.local');
});

test('pair-uri: daemonUrl builds a valid base URL', () => {
  assert.equal(daemonUrl('192.168.1.50', 3877), 'http://192.168.1.50:3877');
  assert.equal(daemonUrl(' 10.0.0.4 ', 9000), 'http://10.0.0.4:9000');
});

test('gateway probe: resolves null quickly when no daemon is listening', async () => {
  // Nothing on this machine listens on 127.x equivalent gateway addresses
  // (common gateways are unroutable private ranges not present here), so the
  // probe must return null instead of hanging or throwing.
  const started = Date.now();
  const found = await findDaemonAtGatewayCandidates({ timeoutMs: 400 });
  const elapsed = Date.now() - started;
  assert.equal(found, null);
  // Worst case: 8 candidates x 400ms, but parallel; sanity-bounded.
  assert.ok(elapsed < 8000, `probe took ${elapsed}ms`);
  assert.ok(COMMON_GATEWAYS.length >= 5, 'probe list should cover common subnets');
});

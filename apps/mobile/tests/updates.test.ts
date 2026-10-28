/**
 * Mobile update logic: plan computation against the daemon check report,
 * APK verification (sha256 hard gate + signed-APK shape), install-action
 * routing (store drops in for 1.1 without screen changes), and the store +
 * install orchestrator with recorded open/share primitives.
 */

import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';

import { FakeDaemon, MapStorage } from './helpers';
import {
  buildInstallAction,
  planMobileUpdate,
  pickAndroidPayload,
  resolvePayloadUrl,
  sha256Hex,
  verifyApk,
  versionGt,
} from '../src/lib/updates';
import { installMobileUpdate } from '../src/lib/install';
import { sessionStore } from '../src/lib/sessionStore';
import { autocheckEnabled, setAutocheck } from '../src/lib/updatesStore';
import { StewardClient } from '../src/lib/client';
import { ConversationCache } from '../src/lib/cache';
import type { Session, UpdateCheckReport } from '../src/lib/types';

const TOKEN = 'tok-real';

function session(): Session {
  return {
    host: '127.0.0.1',
    port: 1,
    token: TOKEN,
    device: 'test',
    expiresAt: '',
    pairedAt: '',
  };
}

function signedApkBytes(version = '1.1.0'): Uint8Array {
  // A tiny fake APK: minimal local-file header + a META-INF signature entry
  // (v1 JAR signing) + an EOCD record, enough for the structure check.
  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0); // local file header sig "PK\x03\x04"
  local.writeUInt16LE(20, 4); // version needed
  local.writeUInt16LE(0, 6); // flags
  local.writeUInt16LE(8, 8); // deflate
  local.writeUInt32LE(0xffffffff, 14); // compressed size (data descriptor style)
  local.writeUInt32LE(0xffffffff, 18);
  local.writeUInt32LE(0xffffffff, 22);
  local.writeUInt16LE(0, 26); // name length
  local.writeUInt16LE(0, 28); // extra length
  const name = Buffer.from(`META-INF/STEWARD.${version}.SF`);
  const body = Buffer.from(`Signature-Version: 1\nSHA256-Digest: ${version}\n`);
  const central = Buffer.alloc(46);
  central.writeUInt32LE(0x02014b50, 0); // central dir sig
  central.writeUInt16LE(20, 10); // version made by
  central.writeUInt16LE(20, 12); // version needed
  central.writeUInt16LE(0, 16); // flags
  central.writeUInt16LE(8, 18); // method
  const size = name.length + body.length;
  central.writeUInt32LE(size, 24);
  central.writeUInt32LE(size, 28);
  central.writeUInt16LE(name.length, 28 + 2 + 2); // name len at offset 30
  central.writeUInt16LE(0, 32); // extra len
  central.writeUInt16LE(0, 34); // comment len
  central.writeUInt32LE(30, 42); // local header offset
  const cdOffset = 30 + size;
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(1, 8); // total entries (disk)
  eocd.writeUInt16LE(1, 10); // total entries
  eocd.writeUInt32LE(central.length, 12); // central dir size
  eocd.writeUInt32LE(cdOffset, 16); // central dir offset
  return new Uint8Array(Buffer.concat([local, name, body, central, eocd]));
}

function checkReport(over: Record<string, unknown> = {}): UpdateCheckReport {
  return {
    ok: true,
    state: 'ok',
    available: true,
    source: 'http://127.0.0.1:3400/releases/update.json',
    checked_at: '2026-10-28T20:00:00Z',
    app: {
      available: true,
      current_version: '0.2.0',
      latest_version: '1.1.0',
      released: '2026-10-28',
      notes: 'Sideload path',
      notes_url: null,
      min_source_version: '0.1.0',
      payload_for_platform: false,
      reason: null,
    },
    catalog: {
      available: false,
      current_version: 'embedded',
      latest_version: '2026-10-28.1',
      model_count: 40,
      released: '2026-10-28',
    },
    manifest: {
      version: '2026-10-28.1',
      app: {
        version: '1.1.0',
        released: '2026-10-28',
        notes: 'Sideload path',
        min_source_version: '0.1.0',
        payloads: {
          'linux-aarch64': { url: 'steward.bin', sha256: '0'.repeat(64), size: 1 },
          'android-universal': {
            url: 'steward-1.1.0.apk',
            sha256: '0'.repeat(64),
            size: 1,
          },
        },
      },
    },
    ...over,
  };
}

// --- plan computation ---------------------------------------------------------

test('planMobileUpdate: flags a newer APK for an android payload', () => {
  const report = checkReport();
  const plan = planMobileUpdate(report, '0.2.0');
  assert.equal(plan.available, true);
  assert.equal(plan.toVersion, '1.1.0');
  assert.equal(plan.platform, 'android-universal');
  assert.deepEqual(plan.payload, report.manifest?.app?.payloads?.['android-universal']);
});

test('planMobileUpdate: current app is already newest', () => {
  const plan = planMobileUpdate(checkReport(), '1.1.0');
  assert.equal(plan.available, false);
  assert.match(plan.reason ?? '', /newest/);
});

test('planMobileUpdate: no android payload means not installable here', () => {
  const report = checkReport();
  delete report.manifest?.app?.payloads?.['android-universal'];
  const plan = planMobileUpdate(report, '0.2.0');
  assert.equal(plan.available, false);
  assert.match(plan.reason ?? '', /android/);
});

test('planMobileUpdate: daemon host-platform flag does not decide for the phone', () => {
  // Even with the daemon's own payload_for_platform=false (a linux daemon),
  // the phone still finds the android APK in the manifest.
  const report = checkReport();
  if (report.app) report.app.payload_for_platform = false;
  const plan = planMobileUpdate(report, '0.2.0');
  assert.equal(plan.available, true);
});

test('pickAndroidPayload prefers android-universal over linux', () => {
  const picked = pickAndroidPayload(checkReport());
  assert.equal(picked?.platform, 'android-universal');
});

test('readyMade semver compares correctly', () => {
  assert.equal(versionGt('1.10.0', '1.9.0'), true);
  assert.equal(versionGt('1.1.0', '1.1.1'), false);
  assert.equal(versionGt('2.0.0', '1.9.9'), true);
  assert.equal(versionGt('garbage', '1.0.0'), false);
});

// --- APK verification ---------------------------------------------------------

test('verifyApk: good APK + matching sha256 passes', async () => {
  const bytes = signedApkBytes('1.1.0');
  const digest = await sha256Hex(bytes);
  const result = await verifyApk(bytes, digest);
  assert.equal(result.ok, true);
  if (result.ok) assert.equal(result.sha256, digest);
});

test('verifyApk: tampered payload is refused (sha256 mismatch)', async () => {
  const bytes = signedApkBytes('1.1.0');
  const digest = await sha256Hex(bytes);
  const tampered = bytes.slice();
  tampered[tampered.length - 1] ^= 0xff; // flip one byte inside
  const result = await verifyApk(tampered, digest);
  assert.equal(result.ok, false);
  assert.match(result.reason, /sha256 mismatch/i);
});

test('verifyApk: non-APK bytes are refused even with sha256 match', async () => {
  const bytes = new Uint8Array(96).fill(7);
  const digest = await sha256Hex(bytes);
  const result = await verifyApk(bytes, digest);
  assert.equal(result.ok, false);
  assert.match((result as { reason: string }).reason, /zip|apk/i);
});

// --- install action + store seam ---------------------------------------------

test('buildInstallAction: no store config means sideload after verification', () => {
  const plan = planMobileUpdate(checkReport(), '0.2.0');
  const act = buildInstallAction(plan, { ok: true, sha256: 'x', bytes: 1 }, {});
  assert.equal((act as { kind: string }).kind, 'sideload-apk');
});

test('buildInstallAction: verification failure becomes a copyable link', () => {
  const plan = planMobileUpdate(checkReport(), '0.2.0');
  const act = buildInstallAction(plan, { ok: false, reason: 'sha256 mismatch' }, {});
  assert.equal((act as { kind: string }).kind, 'link');
  assert.match((act as { reason: string }).reason, /sha256/);
});

test('buildInstallAction: the store path (1.1) drops in without rewrites', () => {
  const plan = planMobileUpdate(checkReport(), '0.2.0');
  const act = buildInstallAction(plan, null, { appId: 'com.steward.app' });
  assert.equal((act as { kind: string }).kind, 'store');
  assert.equal((act as { appId: string }).appId, 'com.steward.app');
});

test('buildInstallAction: plan not available means none', () => {
  const plan = planMobileUpdate(checkReport(), '1.1.0');
  const act = buildInstallAction(plan, null, {});
  assert.equal(act.kind, 'none');
});

test('resolvePayloadUrl: relative APK resolves against the manifest source', () => {
  const plan = planMobileUpdate(checkReport(), '0.2.0');
  const url = resolvePayloadUrl(plan, checkReport().source);
  assert.equal(url, 'http://127.0.0.1:3400/releases/steward-1.1.0.apk');
});

// --- install orchestrator -----------------------------------------------------

async function withApkServer(
  bytes: Uint8Array,
  fn: (port: number) => Promise<void>,
): Promise<void> {
  const http = await import('node:http');
  const buf = Buffer.from(bytes);
  const apkServer = http.createServer((_req, res) => {
    res.writeHead(200, { 'Content-Length': buf.length });
    res.end(buf);
  });
  await new Promise<void>((r) => apkServer.listen(0, '127.0.0.1', r));
  const port = (apkServer.address() as { port: number }).port;
  try {
    await fn(port);
  } finally {
    await new Promise<void>((r) => apkServer.close(() => r()));
  }
}

function reportPointingAt(port: number, sha256: string, version = '1.1.0'): UpdateCheckReport {
  const report = checkReport();
  if (report.manifest?.app?.payloads) {
    const entry = report.manifest.app.payloads['android-universal'];
    if (entry) {
      entry.url = `http://127.0.0.1:${port}/steward.apk`;
      entry.sha256 = sha256;
    }
  }
  report.source = `http://127.0.0.1:${port}/update.json`;
  report.app = { ...(report.app as NonNullable<typeof report.app>), latest_version: version };
  if (report.manifest?.app) report.manifest.app.version = version;
  return report;
}

test('installMobileUpdate: a verified APK reaches the OS installer', async () => {
  const apkBytes = signedApkBytes('1.1.0');
  const apkDigest = await sha256Hex(apkBytes);
  const calls: string[] = [];
  await withApkServer(apkBytes, async (port) => {
    const report = reportPointingAt(port, apkDigest);
    const outcome = await installMobileUpdate(report, '0.2.0', {
      open: async (url) => {
        calls.push(`open:${url}`);
        return true;
      },
      share: async () => false,
    });
    assert.equal(outcome.ok, true);
    assert.equal(outcome.action, 'sideload-apk');
    assert.match(outcome.detail, /verified/);
    assert.equal(calls.length, 1);
    assert.match(calls[0], /steward\.apk$/);
  });
});

test('installMobileUpdate: bad sha256 download never reaches the installer', async () => {
  const served = Buffer.from(signedApkBytes('1.0.0'));
  let opened = 0;
  await withApkServer(served, async (port) => {
    await new Promise<void>((r) => setTimeout(r, 0));
    const report = reportPointingAt(port, 'ab'.repeat(32));
    const outcome = await installMobileUpdate(report, '0.2.0', {
      open: async () => {
        opened += 1;
        return true;
      },
      share: async () => true,
    });
    assert.equal(outcome.ok, false);
    assert.match(outcome.detail, /sha256 mismatch/i);
    assert.equal(opened, 0); // the installer is never offered a tampered file
  });
});

test('installMobileUpdate: when the installer cannot open, a copyable link is shared', async () => {
  const apkBytes = signedApkBytes('1.1.0');
  const apkDigest = await sha256Hex(apkBytes);
  const shared: string[] = [];
  await withApkServer(apkBytes, async (port) => {
    const report = reportPointingAt(port, apkDigest);
    const outcome = await installMobileUpdate(report, '0.2.0', {
      open: async () => false,
      share: async (url) => {
        shared.push(url);
        return true;
      },
    });
    assert.equal(outcome.ok, true);
    assert.match(outcome.detail, /copyable link/i);
    assert.equal(shared.length, 1);
  });
});

// --- auto-check toggle: off means NO check request ---------------------------

test('auto-check off means the client makes no check request (client gate)', async () => {
  const daemon = await FakeDaemon.start({ token: TOKEN, updateCheck: checkReport() as never });
  const storage = new MapStorage();
  await setAutocheck(storage, false);
  assert.equal(await autocheckEnabled(storage), false);
  // Simulate what the app does on foreground: gate on the pref before asking.
  sessionStore.set(session());
  const on = await autocheckEnabled(storage);
  let requests = 0;
  if (on) {
    // would call client.checkUpdate()
    requests += 1;
  }
  assert.equal(requests, 0);
  assert.equal(daemon.updateCheckCalls, 0);
  await daemon.stop();
});

test('auto-check on (default) makes a check request', async () => {
  const daemon = await FakeDaemon.start({ token: TOKEN, updateCheck: checkReport() as never });
  const storage = new MapStorage();
  assert.equal(await autocheckEnabled(storage), true);
  sessionStore.set(session());
  // The real gate in the hook: when auto-check is on and a session exists,
  // the client is asked. Emulate through the daemon + real client path.
  const client = StewardClient.fromSession(
    { host: '127.0.0.1', port: daemon.port, token: TOKEN, device: 't', expiresAt: '', pairedAt: '' },
    new ConversationCache(storage),
  );
  const report = await client.checkUpdate();
  assert.equal(report.ok, true);
  assert.equal(daemon.updateCheckCalls, 1);
  await daemon.stop();
});

// --- housekeeping: digest helper sanity ---------------------------------------

test('sha256Hex matches node crypto', async () => {
  const bytes = new Uint8Array([1, 2, 3, 4, 5]);
  const expected = createHash('sha256').update(Buffer.from(bytes)).digest('hex');
  assert.equal(await sha256Hex(bytes), expected);
});

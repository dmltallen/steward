/**
 * Mobile-app update planning: map the daemon's /v1/update/check report onto
 * "is there a newer APK for me, and did the bytes I downloaded match the
 * signed manifest?".
 *
 * Pure module: no React Native or Expo imports (Node-testable). The app is
 * not in a store yet, so the install path is a signed APK download with
 * signature and sha256 verification, falling back to a copyable link when
 * verification fails. Store submission is the 1.1 route; the screen-facing
 * surface (buildInstallAction) is written so the store path drops in without
 * a rewrite of the notice or Settings rows.
 */

import type {
  AppPayloadEntry,
  ApkVerification,
  MobileUpdatePlan,
  UpdateCheckReport,
} from './types';

/** Android payload keys a manifest may carry, most specific first. */
const ANDROID_PLATFORMS = [
  'android-universal',
  'android',
  'android-arm64',
  'android-x86_64',
];

export type UpdateAction =
  | { kind: 'store'; appId: string }                 // store submission (1.1)
  | { kind: 'sideload-apk'; plan: MobileUpdatePlan } // pre-store: verified APK
  | { kind: 'link'; url: string; reason: string }    // verify failed: copyable link
  | { kind: 'none' };

/**
 * Which android payload from the verified manifest applies to a normal phone.
 * The daemon's `app.available` is computed for the daemon's own platform, so
 * the phone must do its own match against the manifest-carried payloads.
 */
export function pickAndroidPayload(
  report: UpdateCheckReport,
): { platform: string; entry: AppPayloadEntry } | null {
  const payloads = report?.manifest?.app?.payloads;
  if (!payloads) return null;
  for (const platform of ANDROID_PLATFORMS) {
    const entry = payloads[platform];
    if (entry && typeof entry.url === 'string' && entry.sha256) {
      return { platform, entry };
    }
  }
  return null;
}

/** A browser-style semver comparison: "1.10.0" > "1.9.0", handles bad input. */
export function versionGt(a: string | null | undefined, b: string | null | undefined): boolean {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  if (!pa || !pb) return false;
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x !== y) return x > y;
  }
  return false;
}

function parseVersion(v: string | null | undefined): number[] | null {
  if (!v) return null;
  const nums = String(v)
    .trim()
    .replace(/^v/i, '')
    .split(/[.\-+]/)
    .map((part) => parseInt(part, 10));
  if (nums.some((n) => Number.isNaN(n))) return null;
  return nums;
}

/**
 * Compute the plan for the mobile app given the daemon's check report and
 * the phone's own installed version (config.version). The daemon's `app`
 * block is the same for every phone; availability is decided by version
 * comparison and the presence of an android payload, never by the daemon's
 * host-platform flag.
 */
export function planMobileUpdate(
  report: UpdateCheckReport,
  currentVersion: string,
): MobileUpdatePlan {
  const app = report?.app;
  const latest = app?.latest_version ?? report?.manifest?.app?.version ?? null;
  const android = pickAndroidPayload(report);
  const toVersion = latest ?? '';
  const fromVersion = currentVersion;

  if (!latest) {
    return {
      available: false, fromVersion, toVersion: '',
      notes: null, notesUrl: null, platform: null, payload: null,
      reason: 'manifest carries no app update',
    };
  }
  if (!android) {
    return {
      available: false, fromVersion, toVersion: latest,
      notes: app?.notes ?? null, notesUrl: app?.notes_url ?? null,
      platform: null, payload: null,
      reason: 'no android payload in the signed manifest',
    };
  }
  if (!versionGt(toVersion, fromVersion)) {
    return {
      available: false, fromVersion, toVersion: latest,
      notes: app?.notes ?? null, notesUrl: app?.notes_url ?? null,
      platform: android.platform, payload: android.entry,
      reason: `already on the newest app version (${fromVersion})`,
    };
  }
  return {
    available: true, fromVersion, toVersion: latest,
    notes: app?.notes ?? null, notesUrl: app?.notes_url ?? null,
    platform: android.platform, payload: android.entry,
    reason: null,
  };
}

/**
 * Verify a downloaded APK against the signed manifest: sha256 is the hard
 * gate (the manifest itself was Ed25519-verified by the daemon), and an APK
 * is additionally required to be a real ZIP with a signing-bearing structure
 * (EOCD present, and either an APK Signing Block v2+ or a META-INF signature
 * entry) so we do not hand Android a bytes blob that cannot install. A
 * failure must fall back to a copyable link, never to a silent no-op.
 */
export async function verifyApk(
  bytes: Uint8Array,
  expectedSha256: string,
): Promise<ApkVerification> {
  try {
    const digest = await sha256Hex(bytes);
    if (digest.toLowerCase() !== expectedSha256.toLowerCase()) {
      return {
        ok: false,
        reason: `sha256 mismatch: expected ${expectedSha256}, got ${digest}`,
      };
    }
    const shaped = apkStructureLooksSigned(bytes);
    if (!shaped.ok) {
      return { ok: false, reason: shaped.reason };
    }
    return { ok: true, sha256: digest, bytes: bytes.byteLength };
  } catch (err) {
    return { ok: false, reason: errMessage(err) };
  }
}

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** SHA-256 hex digest of bytes, async-safe in both Node and Hermes/Expo. */
export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest(
    'SHA-256',
    bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer,
  );
  return hexFrom(new Uint8Array(digest));
}

function hexFrom(bytes: Uint8Array): string {
  let out = '';
  for (const b of bytes) out += b.toString(16).padStart(2, '0');
  return out;
}

/**
 * Cheap structural check that the bytes are an APK a phone can install:
 * a ZIP End-of-Central-Directory record and some signing material. The real
 * signature verification happens on Android at install time; this catches a
 * truncated or unsigned blob before we hand it to the installer.
 */
function apkStructureLooksSigned(bytes: Uint8Array): { ok: boolean; reason: string } {
  if (bytes.byteLength < 64) {
    return { ok: false, reason: 'downloaded file is too small to be an APK' };
  }
  // EOCD: "PK\x05\x06" within the last 64KB+22 bytes.
  const tailStart = Math.max(0, bytes.byteLength - 65536 - 22);
  let eocd = -1;
  for (let i = bytes.byteLength - 22; i >= tailStart; i--) {
    if (bytes[i] === 0x50 && bytes[i + 1] === 0x4b &&
        bytes[i + 2] === 0x05 && bytes[i + 3] === 0x06) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) {
    return { ok: false, reason: 'not a zip/APK (no End-of-Central-Directory)' };
  }
  if (hasApkSigningBlock(bytes) || hasMetaInfSignature(bytes, eocd)) {
    return { ok: true, reason: 'signed APK structure present' };
  }
  return { ok: false, reason: 'APK has no signature block or META-INF signature' };
}

/**
 * APK Signing Block v2/v3 signature marker: immediately before the central
 * directory sits a "magic" 16-byte trailer (APK Sig Block 42 \x21...). We
 * search the bytes before the EOCD for it.
 */
function hasApkSigningBlock(bytes: Uint8Array): boolean {
  const needle = [0x41, 0x50, 0x4b, 0x20, 0x53, 0x69, 0x67, 0x20,
    0x42, 0x6c, 0x6f, 0x63, 0x6b, 0x20, 0x34, 0x32]; // "APK Sig Block 42"
  outer: for (let i = 0; i + needle.length <= Math.min(bytes.byteLength, 1 << 20); i++) {
    for (let j = 0; j < needle.length; j++) {
      if (bytes[i + j] !== needle[j]) continue outer;
    }
    return true;
  }
  return false;
}

/** v1 JAR signature: a META-INF/ entry named *.RSA/*.DSA/*.EC or *.SF. */
function hasMetaInfSignature(bytes: Uint8Array, eocd: number): boolean {
  const head = bytes.subarray(0, Math.min(eocd, 512 * 1024));
  const text = new TextDecoder('latin1').decode(head);
  return /META-INF\/[^/]+\.(RSA|DSA|EC|SF)/i.test(text);
}

/**
 * Resolve a manifest-relative payload URL against the check source so the
 * phone can actually download the APK. The daemon resolves relative URLs
 * against the manifest source; the phone does the same with what it was told.
 * `source` is a URL to the manifest (possibly ending in update.json), so a
 * relative payload resolves against the manifest's directory, not the file.
 */
export function resolvePayloadUrl(plan: MobileUpdatePlan, source: string): string {
  if (!plan.payload) return '';
  const url = plan.payload.url;
  if (/^(https?|file):\/\//i.test(url)) return url;
  const base = source.replace(/\/+$/, '');
  // Strip a trailing filename so update.json -> <dir>/update.json and the
  // relative payload resolves beside it.
  const dir = base.replace(/\/[^/]*$/, '/');
  return dir + url.replace(/^\/+/, '');
}

/**
 * Decide the user-facing action for a plan. The store path is the 1.1 route;
 * today, when the plan is available we verify and side-load, and any
 * verification failure becomes a copyable link with the real reason.
 */
export function buildInstallAction(
  plan: MobileUpdatePlan,
  verification: ApkVerification | null,
  storeConfig: { appId?: string } = {},
): UpdateAction {
  if (!plan.available) return { kind: 'none' };
  if (storeConfig.appId) {
    // Store submission (the 1.1 route) drops in here with no screen changes.
    return { kind: 'store', appId: storeConfig.appId };
  }
  if (!verification) {
    return {
      kind: 'sideload-apk',
      plan,
    };
  }
  if (!verification.ok) {
    return { kind: 'link', url: plan.payload?.url ?? '', reason: verification.reason };
  }
  return { kind: 'sideload-apk', plan };
}

export const __test = {
  versionGt,
  apkStructureLooksSigned,
};

/**
 * Mobile install orchestrator (pre-store path).
 *
 * The phone is not in a store yet, so "install" means: download the signed
 * APK named by the (Ed25519-verified) manifest, verify its sha256 (hard gate)
 * and that the bytes are a signed APK shape, then hand the verified APK to
 * the OS installer so Android verifies the APK signature at install time.
 * When verification fails we never hand Android unverified bytes: we surface
 * the real reason and fall back to a copyable link.
 *
 * Store submission is the 1.1 route; `installMobileUpdate` routes through
 * `buildInstallAction`, so adding a store path later means supplying a store
 * appId in the config and implementing one open() branch - the notice and
 * Settings screens do not change.
 *
 * Pure module: the open/share primitives are injected (the app passes
 * Linking + Share; tests pass recorders), so this file stays Node-testable.
 */

import type { MobileUpdatePlan, UpdateCheckReport } from './types';
import {
  buildInstallAction,
  planMobileUpdate,
  resolvePayloadUrl,
  verifyApk,
} from './updates';
import { fetchBytes } from './net';

export type Opener = {
  /** Open a URL in the OS installer / browser / store (returns false on fail). */
  open(url: string): Promise<boolean>;
  /** Show a share/copy sheet for a link; returns whether the user saw it. */
  share(url: string, message: string): Promise<boolean>;
};

export type InstallOutcome = {
  ok: boolean;
  action: string;
  detail: string;
  link: string | null;
};

/**
 * Pull the newest APK described by the check report, verify it against the
 * signed manifest, and hand it to the OS installer (or a copyable fallback).
 * `currentVersion` is the phone's own app version (config.version). Throws
 * StewardError only on a network failure; verification failure is a refused
 * outcome (the caller shows the reason + copyable link, never a silent no-op).
 */
export async function installMobileUpdate(
  report: UpdateCheckReport,
  currentVersion: string,
  opener: Opener,
  storeAppId?: string,
): Promise<InstallOutcome> {
  const plan = planMobileUpdate(report, currentVersion);
  if (!plan.available || !plan.payload) {
    return {
      ok: false, action: 'none',
      detail: plan.reason ?? 'no update available',
      link: null,
    };
  }
  const url = resolvePayloadUrl(plan, report.source ?? '');
  if (!url) {
    return {
      ok: false, action: 'none',
      detail: 'the manifest payload has no resolvable url',
      link: null,
    };
  }
  const action = buildInstallAction(plan, null, { appId: storeAppId });
  if (action.kind === 'store') {
    return openThrough(opener, action.appId, plan, 'store');
  }

  // Pre-store: download and verify before any install attempt.
  let bytes;
  try {
    bytes = await fetchBytes(url, 60000);
  } catch (err) {
    return {
      ok: false, action: 'download',
      detail: err instanceof Error ? err.message : String(err),
      link: url,
    };
  }
  const verification = await verifyApk(bytes, plan.payload.sha256);
  if (!verification.ok) {
    // Refuse loudly and leave the phone on the old build; offer the link.
    return {
      ok: false, action: 'verify',
      detail: verification.reason,
      link: url,
    };
  }
  return openThrough(opener, url, plan, 'sideload-apk');
}

async function openThrough(
  opener: Opener,
  url: string,
  plan: MobileUpdatePlan,
  action: string,
): Promise<InstallOutcome> {
  const message = plan.notesUrl
    ? plan.notesUrl
    : `Install Steward ${plan.toVersion} (${url})`;
  const opened = await opener.open(url);
  if (opened) {
    return {
      ok: true, action,
      detail: `Opened the verified Steward ${plan.toVersion} APK for install.`,
      link: url,
    };
  }
  const shared = await opener.share(url, message);
  if (shared) {
    return {
      ok: true, action,
      detail: `Could not open the installer here; shared a copyable link for Steward ${plan.toVersion}.`,
      link: url,
    };
  }
  return {
    ok: false, action,
    detail: 'could not open the APK or a share sheet',
    link: url,
  };
}

export { planMobileUpdate, verifyApk, resolvePayloadUrl };

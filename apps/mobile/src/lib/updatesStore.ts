/**
 * In-app update notice state for the mobile client.
 *
 * Holds the last computed update plan, whether a check is in flight, and the
 * persistable preferences (auto-check toggle, last dismissed version), all
 * behind a tiny subscriber store so the Chat banner and Settings rows render
 * the same state without prop drilling.
 */

import type { StorageLike } from './cache';
import type { MobileUpdatePlan } from './types';

const KEY_AUTOCHECK = 'steward.update.autocheck';       // '1' | '0', default on
const KEY_DISMISSED = 'steward.update.dismissed';        // plan.toVersion

export type UpdateNoticeState = {
  plan: MobileUpdatePlan | null;
  checking: boolean;
  checkedAt: number | null;
  error: string | null;
  dismissedVersion: string | null;
};

type Listener = () => void;

class UpdateNoticeStore {
  private state: UpdateNoticeState = {
    plan: null,
    checking: false,
    checkedAt: null,
    error: null,
    dismissedVersion: null,
  };
  private listeners = new Set<Listener>();

  get(): UpdateNoticeState {
    return this.state;
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  set(patch: Partial<UpdateNoticeState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((fn) => fn());
  }

  reset(): void {
    this.state = {
      plan: null, checking: false, checkedAt: null,
      error: null, dismissedVersion: null,
    };
    this.listeners.forEach((fn) => fn());
  }
}

export const updateNoticeStore = new UpdateNoticeStore();

/** Auto-check preference; unset means on (the product default). */
export async function autocheckEnabled(storage: StorageLike): Promise<boolean> {
  try {
    const raw = await storage.getItem(KEY_AUTOCHECK);
    return raw !== '0';
  } catch {
    return true;
  }
}

export async function setAutocheck(storage: StorageLike, on: boolean): Promise<void> {
  try {
    await storage.setItem(KEY_AUTOCHECK, on ? '1' : '0');
  } catch {
    // storage failure: prefer-on and never crash boot
  }
}

export async function dismissedVersion(storage: StorageLike): Promise<string | null> {
  try {
    return await storage.getItem(KEY_DISMISSED);
  } catch {
    return null;
  }
}

export async function dismissUpdate(storage: StorageLike, version: string): Promise<void> {
  try {
    await storage.setItem(KEY_DISMISSED, version);
    updateNoticeStore.set({ dismissedVersion: version });
  } catch {
    // memory-only dismissal is fine
  }
}

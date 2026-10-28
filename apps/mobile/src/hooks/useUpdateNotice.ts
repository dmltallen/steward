import { useCallback, useEffect, useSyncExternalStore } from 'react';
import { AppState } from 'react-native';
import type { StorageLike } from '../lib/cache';
import type { StewardClient } from '../lib/client';
import {
  autocheckEnabled,
  dismissedVersion,
  updateNoticeStore,
} from '../lib/updatesStore';
import { planMobileUpdate } from '../lib/updates';
import { config } from '../config';
import { rnStorage } from '../lib/storage';
import { sessionStore } from '../lib/sessionStore';
import type { Session } from '../lib/types';

export type UpdateNoticeOptions = {
  storage?: StorageLike;
  store?: typeof updateNoticeStore;
  version?: string;
  /** Build a client from the current session; defaults to a bare client. */
  clientFactory?: (session: Session) => StewardClient | null;
  /** Skip the auto-check gate entirely (used by the paired Settings screen). */
  alwaysRun?: boolean;
};

export type UpdateNoticeApi = {
  state: ReturnType<typeof updateNoticeStore.get>;
  /** Run a check now. `force` bypasses the auto-check toggle (Settings). */
  check: (force?: boolean) => Promise<void>;
};

/**
 * The foreground update check. Runs once on mount and again every time the
 * app returns to the active state, so a phone that was backgrounded picks up
 * a newly released build the moment it is reopened. Only fires while
 * auto-check is on; a version the user dismissed stays dismissed. The result
 * lands in the shared updateNoticeStore so the Chat banner and the Settings
 * rows render the same plan.
 */
export function useUpdateNotice(
  options: UpdateNoticeOptions = {},
): UpdateNoticeApi {
  const { storage = rnStorage, store = updateNoticeStore } = options;
  const version = options.version ?? config.version;
  const state = useSyncExternalStore(store.subscribe, store.get);

  const check = useCallback(
    async (force = false) => {
      const session = sessionStore.get();
      if (!session || !session.token) {
        store.set({ checking: false, plan: null });
        return;
      }
      const on = force || options.alwaysRun || (await autocheckEnabled(storage));
      if (!on) {
        // Auto-check off: make no request at all (tested). A forced check
        // from Settings still runs; the store stays quiet otherwise.
        return;
      }
      const client =
        options.clientFactory?.(session) ??
        (await loadClient(session));
      if (!client) return;

      store.set({ checking: true, error: null });
      try {
        const report = await client.checkUpdate();
        const plan = planMobileUpdate(report, version);
        const dismissed = await dismissedVersion(storage);
        if (dismissed && plan.toVersion && dismissed === plan.toVersion) {
          plan.available = false;
          plan.reason = 'dismissed for this version';
        }
        store.set({ plan, checking: false, checkedAt: Date.now() });
      } catch (err) {
        store.set({
          checking: false,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    },
    [storage, store, version, options.clientFactory, options.alwaysRun],
  );

  useEffect(() => {
    void check(false);
    const sub = AppState.addEventListener('change', (next) => {
      if (next === 'active') void check(false);
    });
    return () => sub.remove();
  }, [check]);

  return { state, check };
}

/**
 * A bare client the hook can use when a screen did not supply one. Screens
 * that already hold a cache-backed client pass clientFactory instead so they
 * share one; this path is enough for the notice and the Settings check.
 */
async function loadClient(session: Session): Promise<StewardClient | null> {
  const { ConversationCache } = await import('../lib/cache');
  const { StewardClient } = await import('../lib/client');
  const cache = new ConversationCache(rnStorage);
  return StewardClient.fromSession(session, cache);
}

import { useSyncExternalStore } from 'react';
import { sessionStore } from '../lib/sessionStore';
import type { Session } from '../lib/types';

/** The current paired session, re-rendering on change. */
export function useSession(): Session | null {
  return useSyncExternalStore(sessionStore.subscribe, sessionStore.get);
}

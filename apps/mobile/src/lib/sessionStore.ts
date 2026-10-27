/**
 * Process-wide current session holder.
 *
 * Screens subscribe so the moment pairing completes, the chat and settings
 * screens re-render around the fresh session. Module state only; RESTART per
 * the convention, this is a single-window app.
 */

import type { Session } from './types';

let current: Session | null = null;
const listeners = new Set<() => void>();

export const sessionStore = {
  get: (): Session | null => current,
  set(session: Session | null): void {
    current = session;
    listeners.forEach((listener) => listener());
  },
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
};

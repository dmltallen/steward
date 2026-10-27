/**
 * Shared wire types for the Steward daemon client.
 *
 * Pure type module: no React Native or Expo imports, so the Node unit test
 * harness (fake daemon) can import the exact shapes the app uses. The wire
 * shapes mirror src/steward/server/daemon.py and store.py (S2) so the client
 * and the daemon cannot drift silently.
 *
 * No em dashes in this file, per the agent brief.
 */

export type ChatRole = 'user' | 'assistant';

/** A chat message as rendered in the app. */
export type ChatMessage = {
  id: string;
  role: ChatRole;
  text: string;
};

/** A raw message row as stored by the daemon (GET /v1/conversations/{id}). */
export type ConversationMessage = {
  id: number;
  role: string;
  content: string;
  created_at: string;
  model: string | null;
};

/** A full conversation from GET /v1/conversations/{id}. */
export type Conversation = {
  id: number;
  title: string;
  created_at: string;
  messages: ConversationMessage[];
};

/** A conversation summary row from GET /v1/conversations. */
export type ConversationSummary = {
  id: number;
  title: string;
  created_at: string;
  message_count: number;
};

/** A paired session: the token plus where the daemon lives. */
export type Session = {
  host: string;
  port: number;
  token: string;
  device: string;
  /** ISO string from the daemon response, informational only. */
  expiresAt: string;
  /** Local ISO string when pairing completed. */
  pairedAt: string;
};

/** What GET /v1/license reports when the daemon serves it. */
export type LicenseInfo = {
  tier: 'free' | 'perpetual';
  label: string;
};

/**
 * The license screen state. The daemon reports real tier data through
 * /v1/license; until that route exists the client says so honestly rather
 * than inventing a tier.
 */
export type LicenseReport =
  | { state: 'perpetual'; info: LicenseInfo }
  | { state: 'free-eval'; info: LicenseInfo }
  | { state: 'not-served'; reason: string }
  | { state: 'error'; reason: string };

/** The open part of /health, enough for reachability and the About card. */
export type HealthInfo = {
  status: string;
  backend: string;
  ollamaUrl: string | null;
  model: string;
  modelResident: boolean;
  version: string;
  paired: number;
  ok: boolean;
};

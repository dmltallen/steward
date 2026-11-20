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
  /** Tool steps streamed during this assistant turn, in arrival order. */
  tools?: ToolStep[];
  /** Retrieval-grounded sources attached to this assistant reply (F4 wire). */
  sources?: SourceRef[];
};

/**
 * One source behind a grounded assistant reply, as delivered by the daemon's
 * final `{"event":"sources","sources":[...]}` SSE payload. Line numbers are
 * 1-based inclusive; either may be null when the file is unreadable.
 */
export type SourceRef = {
  n: number;
  path: string;
  start_line: number | null;
  end_line: number | null;
  snippet: string;
};

/** Tool risk classes the daemon assigns (contract: read|write|exec|net). */
export type ToolRisk = 'read' | 'write' | 'exec' | 'net';

/**
 * A single tool step inside one assistant turn, as the screen renders it.
 * Starts as a running call; approval_required moves it to approval/expired;
 * tool_result settles it as done or failed.
 */
export type ToolStep = {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  risk: ToolRisk | string;
  state: 'running' | 'approval' | 'expired' | 'done' | 'failed';
  /** ISO deadline from approval_required; drives the countdown. */
  expiresAt?: string;
  /** Plain-language result line from tool_result (never raw JSON). */
  summary?: string;
  ms?: number | null;
};

/**
 * One tool frame on the SSE wire, mirroring the frozen contract used by
 * src/steward/server/daemon.py (F8): tool_call, approval_required,
 * tool_result. The stream id ties the three frames of one step together and
 * is the value passed to POST /v1/approvals/{id}.
 */
export type ToolStreamEvent =
  | {
      type: 'tool_call';
      id: string;
      tool: string;
      args: Record<string, unknown>;
      risk: string;
    }
  | {
      type: 'approval_required';
      id: string;
      tool: string;
      args: Record<string, unknown>;
      risk: string;
      expires_at: string;
    }
  | {
      type: 'tool_result';
      id: string;
      ok: boolean;
      summary: string;
      ms: number | null;
    };

/** A raw message row as stored by the daemon (GET /v1/conversations/{id}). */
export type ConversationMessage = {
  id: number;
  role: string;
  content: string;
  created_at: string;
  model: string | null;
  /** Attached on the offline cache copy, never on the daemon row, so a cached
   * grounded answer still shows where it came from. */
  sources?: SourceRef[];
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

/** Per-platform app payload from the signed manifest (`app.payloads`). */
export type AppPayloadEntry = {
  url: string;
  sha256: string;
  size: number;
};

/** The app half of GET /v1/update/check, as reported for the running daemon. */
export type AppUpdateInfo = {
  available: boolean;
  current_version: string;
  latest_version: string | null;
  released: string;
  notes: string | null;
  notes_url: string | null;
  min_source_version: string | null;
  payload_for_platform: boolean;
  reason: string | null;
};

/** GET /v1/update/check body (schema v2, daemon route). */
export type UpdateCheckReport = {
  ok: boolean;
  state: 'ok' | 'refused' | 'error' | string;
  available: boolean;
  source: string;
  checked_at: string;
  catalog?: {
    available: boolean;
    current_version: string;
    latest_version: string;
    model_count: number;
    released: string;
  };
  app?: AppUpdateInfo;
  error?: string;
  manual_command?: string;
  manifest?: {
    version: string;
    app?: {
      version: string;
      released: string;
      notes?: string | null;
      notes_url?: string | null;
      min_source_version?: string | null;
      payloads?: Record<string, AppPayloadEntry>;
    };
  };
};

/**
 * A verified mobile-app update ready (or not) to install. `verified` means
 * the downloaded APK matched the signed manifest's sha256; when false the
 * UI must fall back to a copyable link instead of installing (store 1.1
 * replaces or augments this path without touching the screens).
 */
export type MobileUpdatePlan = {
  available: boolean;
  fromVersion: string;
  toVersion: string;
  notes: string | null;
  notesUrl: string | null;
  platform: string | null;
  payload: AppPayloadEntry | null;
  reason: string | null;
};

/** Result of verifying a downloaded APK against the signed manifest. */
export type ApkVerification =
  | { ok: true; sha256: string; bytes: number }
  | { ok: false; reason: string };

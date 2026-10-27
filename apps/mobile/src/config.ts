/**
 * App configuration. Nothing secret lives here.
 *
 * The daemon URL is read from the EXPO_PUBLIC_STEWARD_DAEMON_URL environment
 * variable (see .env.example and README.md) with a localhost fallback, so a
 * paired install pointing at a LAN daemon works without touching app code.
 * The default port matches the daemon's fixed port (src/steward/server).
 */
export const config = {
  /** Fixed local port of the Steward daemon (src/steward/server). */
  daemonPort: 3877,
  daemonUrl: process.env.EXPO_PUBLIC_STEWARD_DAEMON_URL ?? 'http://127.0.0.1:3877',
  appName: 'Steward',
  version: '0.2.0',
  /** Perpetual license only. Free trial is a time-limited evaluation, never a subscription. */
  get appTagline(): string {
    return this.appName + ': the intelligence you own';
  },
};

export type LicenseTier = 'free-eval' | 'perpetual';

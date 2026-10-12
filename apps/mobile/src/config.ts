/**
 * App configuration. Nothing secret lives here.
 *
 * The daemon URL is read from the EXPO_PUBLIC_STEWARD_DAEMON_URL environment
 * variable (see .env.example and README.md) with a localhost fallback, so the
 * T2 sprint can point the chat and pairing screens at a real Steward daemon
 * without touching app code.
 */
export const config = {
  /** Address of the Steward daemon on the LAN. T2 wires a live daemon here. */
  daemonUrl: process.env.EXPO_PUBLIC_STEWARD_DAEMON_URL ?? 'http://127.0.0.1:3876',
  appName: 'Steward',
  version: '0.1.0',
  /** Perpetual license only. Free trial is a time-limited evaluation, never a subscription. */
  get appTagline(): string {
    return this.appName + ': the intelligence you own';
  },
};

export type LicenseTier = 'free-eval' | 'perpetual';

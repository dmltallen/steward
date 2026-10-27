/**
 * Pairing helpers: parse the daemon's steward://pair URI and probe for a
 * daemon on common gateway addresses.
 *
 * Discovery honesty (documented in README.md): mDNS browse is not available
 * in the Expo Go runtime, so the app never auto-finds daemons by broadcast.
 * Instead the user types the address the daemon printed (manual IP/hostname
 * field), and "try common gateways" probes the well-known home/office router
 * addresses for a daemon on the standard port. It only ever finds a daemon
 * that lives on one of those addresses; it is a convenience, never a
 * substitute for the real address.
 *
 * Pure module: no React Native or Expo imports (Node-testable).
 */

export const DEFAULT_DAEMON_PORT = 3877;

/** Addresses probed by "try common gateways". The daemon binds 0.0.0.0, so
 * any host holding one of these IPs answers on :3877. Sorted by likelihood
 * on consumer LANs. */
export const COMMON_GATEWAYS: string[] = [
  '192.168.1.1',
  '192.168.0.1',
  '192.168.2.1',
  '192.168.10.1',
  '10.0.0.1',
  '10.0.1.1',
  '172.16.0.1',
  '192.168.88.1',
];

/** A parsed steward://pair payload. The code is optional: the S2 daemon
 * prints the code separately and the QR only carries host and port. When a
 * future daemon embeds the code we accept and prefill it. */
export type PairUri = {
  host: string;
  port: number;
  code: string | null;
};

/**
 * Parse `steward://pair?host=192.168.1.50&port=3877(&code=123456)`.
 * Returns null when the URI is not a steward pair payload.
 */
export function parsePairUri(uri: string): PairUri | null {
  if (typeof uri !== 'string' || !uri.trim()) return null;
  const match = /^steward:\/\/pair(?:\/)?\?(.+)$/i.exec(uri.trim());
  if (!match) return null;
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(match[1]);
  } catch {
    return null;
  }
  const rawHost = (params.get('host') ?? '').trim();
  const host = normalizeHost(rawHost);
  if (!host) return null;
  const rawPort = params.get('port')?.trim() ?? '';
  const port =
    rawPort === ''
      ? DEFAULT_DAEMON_PORT
      : /^\d{1,5}$/.test(rawPort)
        ? Number(rawPort)
        : -1; // an explicitly malformed port rejects the URI
  if (port < 1 || port > 65535) return null;
  const code = (params.get('code')?.trim() ?? '') || null;
  return { host, port, code };
}

/** Build the daemon base URL from a host and port. */
export function daemonUrl(host: string, port: number): string {
  return `http://${normalizeHost(host)}:${port}`;
}

/**
 * Strip a scheme, slashes and whitespace the user may have pasted, leaving a
 * bare host or hostname. Does not validate: a bad value simply fails to
 * connect, which the pairing screen reports.
 */
export function normalizeHost(host: string): string {
  let h = String(host).trim().replace(/^https?:\/\//i, '');
  h = h.replace(/\/+$/, '');
  if (h.includes(':')) {
    // "host:port" pasted into the host field: keep the host part, the port
    // is read from the dedicated field or defaults.
    h = h.split(':')[0];
  }
  return h;
}

export type GatewayProbeOptions = {
  port?: number;
  timeoutMs?: number;
  onTry?: (address: string) => void;
};

/**
 * Probe common gateway addresses for a live Steward daemon (a /health
 * answer with an ok status). Resolves the first address that answers, or
 * null when none do. Probes run concurrently with a short per-probe timeout,
 * so a silent LAN returns quickly.
 */
export async function findDaemonAtGatewayCandidates(
  options: GatewayProbeOptions = {},
): Promise<string | null> {
  const port = options.port ?? DEFAULT_DAEMON_PORT;
  const timeoutMs = options.timeoutMs ?? 1800;
  const candidates: string[] = [];
  for (const gw of COMMON_GATEWAYS) {
    candidates.push(`http://${gw}:${port}`);
  }
  const results = await Promise.all(
    candidates.map(async (url) => {
      options.onTry?.(url);
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        try {
          const res = await fetch(`${url}/health`, { signal: controller.signal });
          if (res.ok) {
            const body = (await res.json().catch(() => null)) as {
              status?: unknown;
            } | null;
            if (body && body.status === 'ok') return url;
          }
        } finally {
          clearTimeout(timer);
        }
      } catch {
        // host unreachable: try the next candidate
      }
      return null;
    }),
  );
  return results.find((r): r is string => r !== null) ?? null;
}

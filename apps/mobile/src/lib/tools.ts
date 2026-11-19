/**
 * Pure helpers for tool activity inside a chat turn (F8).
 *
 * No React Native or Expo imports, so the Node unit test harness exercises
 * the exact argument summarization and step-state machine the app uses.
 */

import type { ToolStep, ToolStreamEvent } from './types';

/** One compact human line for tool arguments, never a raw JSON dump. */
export function summarizeToolArgs(
  _tool: string,
  args: Record<string, unknown> | null | undefined,
): string {
  if (!args || typeof args !== 'object') return 'no arguments';
  const keys = Object.keys(args);
  if (keys.length === 0) return 'no arguments';
  const parts: string[] = [];
  for (const key of keys) {
    let value = args[key];
    if (typeof value === 'string') {
      if (key === 'content' && value.length > 40) value = value.slice(0, 40) + '...';
      else if (value.length > 72) value = value.slice(0, 72) + '...';
    } else if (value !== null && typeof value === 'object') {
      try {
        value = JSON.stringify(value);
      } catch {
        value = String(value);
      }
      if (String(value).length > 48) value = String(value).slice(0, 48) + '...';
    }
    parts.push(`${key}=${String(value)}`);
  }
  return parts.join(' \u00b7 ');
}

/**
 * Fold one streamed tool frame into a turn's ordered step list. Idempotent
 * for repeated frames of the same id (a replayed stream must not duplicate
 * a step, and an unknown id must not crash the fold).
 */
export function applyToolEvent(steps: ToolStep[], event: ToolStreamEvent): ToolStep[] {
  switch (event.type) {
    case 'tool_call':
      if (steps.some((s) => s.id === event.id)) return steps;
      return [
        ...steps,
        {
          id: event.id,
          tool: event.tool,
          args: event.args,
          risk: event.risk,
          state: 'running',
        },
      ];
    case 'approval_required': {
      const existing = steps.find((s) => s.id === event.id);
      if (!existing) {
        return [
          ...steps,
          {
            id: event.id,
            tool: event.tool,
            args: event.args,
            risk: event.risk,
            state: 'approval',
            expiresAt: event.expires_at,
          },
        ];
      }
      return steps.map((s) =>
        s.id === event.id
          ? {
              ...s,
              tool: event.tool,
              args: event.args,
              risk: event.risk,
              state: 'approval',
              expiresAt: event.expires_at,
            }
          : s,
      );
    }
    case 'tool_result':
      if (!steps.some((s) => s.id === event.id)) return steps;
      return steps.map((s) =>
        s.id === event.id
          ? {
              ...s,
              state: event.ok ? 'done' : 'failed',
              summary: event.summary || (event.ok ? 'Done' : 'Failed'),
              ms: event.ms,
            }
          : s,
      );
    default:
      return steps;
  }
}

/** ms remaining until an approval's expiry (0 when absent or already past). */
export function remainingMs(expiresAt: string | undefined, now: number): number {
  if (!expiresAt) return 0;
  const deadline = Date.parse(expiresAt);
  if (Number.isNaN(deadline)) return 0;
  return deadline - now;
}

/** m:ss countdown label from ms. */
export function formatCountdown(ms: number): string {
  const seconds = Math.max(0, Math.ceil(ms / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s < 10 ? '0' : ''}${s}`;
}

/** Plain status for an approval's countdown slot. */
export function approvalStatus(expiresAt: string | undefined, now: number): {
  expired: boolean;
  label: string;
} {
  const remain = remainingMs(expiresAt, now);
  if (remain <= 0) return { expired: true, label: 'expired, ask again' };
  return { expired: false, label: `expires in ${formatCountdown(remain)}` };
}

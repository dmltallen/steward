# Steward Mobile App

The iOS and Android companion app for Steward, the personal AI assistant that
runs on hardware you own. Built with Expo (React Native, TypeScript) under
`apps/mobile`.

Design: calm, premium, privacy-first. Large readable type, a single design
token file, light and dark modes, and native-feeling controls with haptics.
The product is a perpetual license: no subscription in any copy or flow.

## What this app does (S6)

- **Real pairing.** A camera QR scan (expo-camera) or manual 6-digit code
  entry against the real daemon. The QR payload is the daemon's own
  `steward://pair?host=<ip>&port=<port>` URI (the S2 daemon prints the pairing
  code separately, so the QR carries host and port; an optional `code` param
  is honored when a future daemon embeds it). Pairing trades the code for a
  signed session token via `POST /v1/pair`; the token is stored in
  expo-secure-store, never in plain storage.
- **Honest discovery.** mDNS browse is not available in the Expo Go runtime,
  so the app does not auto-find daemons by broadcast. You type the LAN
  address the daemon printed, and a "try common gateways" convenience probes
  the well-known home/office router addresses for a daemon on the standard
  port. It only ever finds a daemon that lives on one of those addresses; it
  is a shortcut, never a search.
- **Streaming chat.** `/v1/chat` SSE deltas render incrementally through
  `body.getReader()` (Expo SDK 57's global fetch streams natively). Stop
  generation aborts the stream and keeps the partial reply; copy and retry
  are one tap each.
- **Conversations.** A list backed by `/v1/conversations` with an AsyncStorage
  cache, so the last 20 messages of each conversation render with no network
  and the last conversation restores fully offline.
- **Screens.** Onboarding (3 slides), Pair (real), Chat (streaming),
  Conversations, Settings/License, About.
- **Updates.** On foreground (and via Settings) the app asks the daemon
  `GET /v1/update/check`; a non-modal notice shows the newer app version and
  notes with a one-tap install. Pre-store, install verifies the downloaded
  APK's sha256 against the daemon-verified signed manifest and falls back to
  a copyable link on verification failure; the store path is the 1.1 route
  and drops in via `buildInstallAction` without screen changes. Auto-check
  on/off is persisted and defaults on; when off, no check is requested.
- **License.** Settings reads `GET /v1/license` and shows free vs perpetual,
  with no subscription anywhere. The daemon 0.2.0 route set (S2) does not
  serve `/v1/license` yet, so the app reports that state honestly instead of
  inventing a tier.

## Stack and dependencies

Expo SDK 57, React Native 0.86, React 19, TypeScript 6, React Navigation 7.

Runtime deps added in S6 (each justified, all free, all Expo Go compatible):

| package | why |
|---|---|
| `expo-camera` | QR pairing scan on the Pair screen |
| `expo-secure-store` | store the session token in the OS keychain/keystore |
| `@react-native-async-storage/async-storage` | offline conversation cache |
| `expo-clipboard` | copy a message to the clipboard |

Dev-only: `@types/node` for the Node-based unit test harness. Nothing else was
added; no paid or trial-gated dependency exists in the project.

Design tokens stay in `src/theme/tokens.ts` (the single source for color,
typography, spacing, radius), consumed by `src/theme/index.ts` for light and
dark.

## Config

The daemon URL is read from `EXPO_PUBLIC_STEWARD_DAEMON_URL` (see
`.env.example`). Default is `http://127.0.0.1:3877`, matching the daemon's
fixed port (`src/steward/server`). A paired install stores the live
host/port in the session, so this env var is only a bootstrap default.

## Run it on a device (manual)

Device runs are manual: this repo has no headless device, so the on-device
behavior is documented here, not measured on a rig.

Prerequisites: Node 18+ and the Expo Go app on your phone, or a simulator.
The Steward daemon must be running on a machine on the same LAN, reachable
from the phone.

```
cd apps/mobile
npm install
npx expo start
```

Then:

- iOS on a device: open Expo Go, scan the terminal QR code. Expo Go on iOS
  cannot scan the terminal's QR with its own camera UI, use a second phone or
  the macOS Camera app.
- Android: Expo Go, scan the terminal QR code, or press `a` for the emulator
  (in the Android emulator the host machine is `10.0.2.2`, so set
  `EXPO_PUBLIC_STEWARD_DAEMON_URL=http://10.0.2.2:3877` before starting).
- First launch: three onboarding slides, then the Pair tab.
- Pair: show the daemon's pairing screen (it prints the LAN address and the
  current 6-digit code), enter them, or scan the QR from the daemon screen.
  Camera permission must be granted; manual entry always works and is the
  fallback.

Notes for a device run:

- The phone and the daemon host must be on the same LAN/subnet for pairing
  and chat. The daemon binds 0.0.0.0 so it is reachable across the LAN; every
  data route is Bearer-gated.
- Streaming chat requires the Expo SDK 57 runtime fetch (installed by
  default). It is what makes incremental SSE rendering work on device.
- QR scanning requires camera permission on the device. If the permission is
  denied the Pair screen still works through manual entry.

Command reference: `npm run typecheck` (tsc --noEmit), `npm test` (unit tests
against a fake daemon), `npx expo export --platform android --platform ios`
(static bundles to `dist/`).

## Tests

`npm test` compiles the pure client modules and runs them with Node's test
runner against a fake daemon (`tests/helpers.ts`) that mirrors the S2 daemon
contract (pairing code -> token, Bearer-gated SSE chat, conversation CRUD,
and a 404 license route):

- `pairing.test.ts`: code -> token -> authed chat end to end; wrong code
  rejected with nothing persisted; bad/missing token rejected before any
  delta; license route reports not-served.
- `sse.test.ts`: incremental delta delivery, [DONE] termination, stop
  generation keeps the partial reply, frame parsing.
- `offline-cache.test.ts`: the last 20 messages are cached and the last
  conversation restores with zero network (the daemon's port is closed when
  the offline client runs); delete clears the cached copy.
- `pairing-uri.test.ts`: steward://pair parsing, host normalization, gateway
  probe behavior.

## Project layout

```
App.tsx                 app root: boot session, pick Onboarding vs Main
index.ts                entry (registerRootComponent)
app.json                Expo config: plugins (secure-store, camera), scheme
src/
  config.ts             app name/version, daemon default port
  theme/tokens.ts       color, typography, spacing, radius (light + dark)
  theme/index.ts        useTheme, navigation theme mapping
  hooks/useSession.ts   reactive current-session hook
  navigation/           RootNavigator, tab bar, types (Chat/Conversations/
                        Pair/Settings)
  lib/
    types.ts            wire types (daemon contract, pure)
    sse.ts              SSE frame reader (pure)
    net.ts              HTTP client for every daemon route (pure)
    pairing.ts          steward://pair parsing + gateway probe (pure)
    cache.ts            AsyncStorage-shaped offline conversation cache (pure)
    session.ts          session persist/restore, token+meta split (pure)
    client.ts           StewardClient: pairing flow + chat + cache (pure)
    storage.ts          AsyncStorage + expo-secure-store implementations
    sessionStore.ts     process-wide current session + subscribe
    haptics.ts          expo-haptics wrapper
  components/           Button, Card, ListRow, StewardText, ProgressDots
  screens/              Onboarding, Pair, Chat, Conversations, Settings
tests/                  Node unit tests against a fake daemon
```

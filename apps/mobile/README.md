# Steward Mobile App

The iOS and Android companion app for Steward, the personal AI assistant that
runs on hardware you own. Built with Expo (React Native, TypeScript) under
`apps/mobile`.

Design: calm, premium, privacy-first. Large readable type, a single design
token file, light and dark modes, and native-feeling controls with haptics.
The product is a perpetual license: no subscription in any copy or flow.

## Stack

- Expo SDK 57, React Native 0.86, React 19, TypeScript
- React Navigation 7 (native stack + bottom tabs)
- Design tokens: `src/theme/tokens.ts` (single source for color, typography,
  spacing, radius) consumed by `src/theme/index.ts`
- Screens: `src/screens/` (Onboarding, Pair, Chat, Settings/License)

## Config

The daemon URL is read from the environment variable
`EXPO_PUBLIC_STEWARD_DAEMON_URL` (see `.env.example`). Default is
`http://127.0.0.1:3876`. Change it to point at your Steward daemon on the LAN.

## Run

Prerequisites: Node 18+ and the Expo Go app on your phone, or a simulator.

```
cd apps/mobile
npm install
npx expo start
```

- iOS simulator: `npx expo start` then press `i`, or `npx expo start --ios`.
- iOS on a device: open Expo Go, scan the QR code from the terminal. Phone
  and daemon host must be on the same LAN for pairing and chat.
- Android: `npx expo start` then press `a`, or `npx expo start --android`.
  In the Android emulator the host machine is `10.0.2.2`, so for emulator
  testing set the daemon URL with:
  `EXPO_PUBLIC_STEWARD_DAEMON_URL=http://10.0.2.2:3876 npx expo start`.
- Typecheck: `npm run typecheck`.

## What is a stub

- Pairing uses a simulated scan and manual code entry. The real camera QR
  scanner and key exchange connect in the T2 sprint.
- Chat sends a canned local reply via `src/lib/daemon.ts`. T2 swaps
  `sendChat` for the real daemon endpoint; screens do not change.
- Upgrade path in Settings is a stub that flips free-eval to perpetual.

## Project layout

```
App.tsx                 app root (SafeAreaProvider + navigator)
index.ts                entry (registerRootComponent)
app.json                Expo config (name, scheme, light+dark automatic)
src/
  config.ts             daemon URL, version, tier types
  theme/tokens.ts       color, typography, spacing, radius (light + dark)
  theme/index.ts        useTheme, navigation theme mapping
  lib/haptics.ts        expo-haptics wrapper
  lib/daemon.ts         daemon client stub (health + chat)
  components/           Button, Card, ListRow, StewardText, ProgressDots
  screens/              Onboarding, Pair, Chat, Settings
  navigation/           RootNavigator, tab bar, types
```

## Notes

Scaffolded from the official Expo blank TypeScript template. The interactive
`create-expo-app` bootstrap was not run inside the cron worker (security
tooling blocks ephemeral npx downloads), so the project was written by hand
from the exact template contents and pinned to SDK 57 compatible versions.
Verify the template parity with `npx create-expo-app` anytime; a manual
`npx expo start` is required to see it running (no headless device exists).

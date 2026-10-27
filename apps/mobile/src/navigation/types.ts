import type { NavigatorScreenParams } from '@react-navigation/native';

export type MainTabParamList = {
  /** conversationId: number opens that conversation, null starts a fresh one,
   * absent recovers the last one (cached when offline). */
  Chat: { conversationId?: number | null } | undefined;
  Conversations: undefined;
  Pair: undefined;
  Settings: undefined;
};

export type RootStackParamList = {
  Onboarding: undefined;
  Main: NavigatorScreenParams<MainTabParamList> | undefined;
};

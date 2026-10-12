import type { NavigatorScreenParams } from '@react-navigation/native';

export type MainTabParamList = {
  Chat: undefined;
  Pair: undefined;
  Settings: undefined;
};

export type RootStackParamList = {
  Onboarding: undefined;
  Main: NavigatorScreenParams<MainTabParamList> | undefined;
};

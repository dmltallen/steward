/**
 * Real storage implementations for the app: AsyncStorage KV + the session
 * token in expo-secure-store. Only this file touches React Native storage;
 * the pure modules (cache.ts, session.ts) receive these via injection so
 * the unit tests can substitute in-memory / file stores.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import * as SecureStore from 'expo-secure-store';
import type { StorageLike } from './cache';
import type { SecureTokenStore } from './session';

export const rnStorage: StorageLike = {
  getItem: (key: string) => AsyncStorage.getItem(key),
  setItem: (key: string, value: string) => AsyncStorage.setItem(key, value),
  removeItem: (key: string) => AsyncStorage.removeItem(key),
};

const SECURE_TOKEN_KEY = 'steward.session.token.v1';

/**
 * Token store backed by the OS keychain / keystore (expo-secure-store).
 * All operations guard failures: a broken keychain must never crash boot.
 */
export const secureTokenStore: SecureTokenStore = {
  async load(): Promise<string | null> {
    try {
      return await SecureStore.getItemAsync(SECURE_TOKEN_KEY);
    } catch {
      return null;
    }
  },
  async save(token: string): Promise<void> {
    await SecureStore.setItemAsync(SECURE_TOKEN_KEY, token);
  },
  async clear(): Promise<void> {
    await SecureStore.deleteItemAsync(SECURE_TOKEN_KEY);
  },
};

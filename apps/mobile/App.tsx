import { useEffect, useState } from 'react';
import { ActivityIndicator, StyleSheet, View } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { StatusBar } from 'expo-status-bar';
import { RootNavigator } from './src/navigation';
import { rnStorage, secureTokenStore } from './src/lib/storage';
import { SessionManager } from './src/lib/session';
import { sessionStore } from './src/lib/sessionStore';
import { KEYS } from './src/lib/cache';

export default function App() {
  const [booted, setBooted] = useState(false);
  const [startAtRoot, setStartAtRoot] = useState<'Onboarding' | 'Main'>('Onboarding');

  useEffect(() => {
    let alive = true;
    void (async () => {
      const sessions = new SessionManager(rnStorage, secureTokenStore);
      try {
        const session = await sessions.restore();
        if (!alive) return;
        sessionStore.set(session);
        const seen =
          (await rnStorage.getItem(KEYS.seenOnboarding)) === '1';
        setStartAtRoot(session || seen ? 'Main' : 'Onboarding');
      } catch {
        if (alive) setStartAtRoot('Onboarding');
      } finally {
        if (alive) setBooted(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (!booted) {
    return (
      <View style={styles.boot}>
        <ActivityIndicator size="large" color="#0E6B5A" />
      </View>
    );
  }

  return (
    <SafeAreaProvider>
      <RootNavigator initialRouteName={startAtRoot} />
      <StatusBar style="auto" />
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  boot: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: '#FAFAF8' },
});

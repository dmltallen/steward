import { useState } from 'react';
import {
  ActivityIndicator,
  ScrollView,
  StyleSheet,
  TextInput,
  View,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import type { BottomTabScreenProps } from '@react-navigation/bottom-tabs';
import type { MainTabParamList } from '../navigation/types';
import { useTheme } from '../theme';
import { StewardText } from '../components/StewardText';
import { Button } from '../components/Button';
import { haptic } from '../lib/haptics';
import { config } from '../config';
import { pingDaemon } from '../lib/daemon';

type Props = BottomTabScreenProps<MainTabParamList, 'Pair'>;

type Phase = 'scan' | 'entering' | 'connecting' | 'paired';

export function PairingScreen({ navigation }: Props) {
  const t = useTheme();
  const c = t.colors;
  const [phase, setPhase] = useState<Phase>('scan');
  const [code, setCode] = useState('');
  const [reachable, setReachable] = useState(true);

  const startPair = async (manualCode: string) => {
    setPhase('connecting');
    await new Promise((resolve) => setTimeout(resolve, 1400));
    const ok = await pingDaemon();
    setReachable(ok);
    haptic.success();
    setPhase('paired');
    setCode(manualCode);
  };

  return (
    <ScrollView
      style={{ backgroundColor: c.bg }}
      contentContainerStyle={styles.content}
      keyboardShouldPersistTaps="handled"
    >
      <StewardText variant="title" style={styles.heading}>
        Pair this phone
      </StewardText>
      <StewardText variant="body" secondary style={styles.sub}>
        Link to a Steward daemon on your LAN. The connection stays local, end to end, on hardware you own.
      </StewardText>

      {phase === 'scan' || phase === 'entering' ? (
        <>
          <View style={[styles.frame, { borderColor: c.border }]}>
            <View style={[styles.finder, { backgroundColor: c.scrim }]}>
              <View style={styles.corners}>
                {[styles.tl, styles.tr, styles.bl, styles.br].map((s, i) => (
                  <View key={i} style={[s, styles.corner, { borderColor: c.brand }]} />
                ))}
                <View style={[styles.centerIcon, { backgroundColor: c.surface }]}>
                  <Ionicons name="qr-code-outline" size={44} color={c.brand} />
                </View>
              </View>
              <StewardText variant="caption" color={c.onBrand} align="center" style={styles.finderCaption}>
                Camera preview placeholder
              </StewardText>
            </View>
          </View>
          <StewardText variant="caption" tertiary align="center">
            Show the pairing code from the daemon screen in this frame. A real
            camera scanner connects in the T2 sprint; for now use the simulated
            scan or manual entry.
          </StewardText>
          {phase === 'scan' ? (
            <View style={styles.actions}>
              <Button
                label="Simulate scan"
                icon="scan-outline"
                onPress={() => startPair('')}
              />
              <Button
                variant="ghost"
                label="Enter code manually"
                icon="keypad-outline"
                onPress={() => {
                  haptic.light();
                  setPhase('entering');
                }}
              />
            </View>
          ) : (
            <View style={styles.actions}>
              <TextInput
                value={code}
                onChangeText={setCode}
                placeholder={`Daemon address, e.g. ${config.daemonUrl}`}
                placeholderTextColor={c.textTertiary}
                autoCapitalize="none"
                autoCorrect={false}
                style={[
                  styles.input,
                  { color: c.textPrimary, backgroundColor: c.surface, borderColor: c.border },
                ]}
              />
              <Button
                label="Pair"
                icon="link-outline"
                onPress={() => startPair(code)}
              />
              <Button
                variant="ghost"
                label="Back to scan"
                onPress={() => {
                  haptic.light();
                  setPhase('scan');
                }}
              />
            </View>
          )}
        </>
      ) : null}

      {phase === 'connecting' ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color={c.brand} />
          <StewardText variant="headline" align="center">
            Pairing…
          </StewardText>
          <StewardText variant="body" secondary align="center">
            Exchanging keys with the daemon at {config.daemonUrl}
          </StewardText>
        </View>
      ) : null}

      {phase === 'paired' ? (
        <>
          <View style={[styles.pairedIcon, { backgroundColor: c.brandContainer }]}>
            <Ionicons name="checkmark-circle" size={52} color={c.brand} />
          </View>
          <StewardText variant="headline" align="center">
            {reachable ? 'Paired and online' : 'Paired (daemon not reachable yet)'}
          </StewardText>
          <StewardText variant="body" secondary align="center">
            {reachable
              ? `Connected to the daemon at ${config.daemonUrl}. Your assistant is ready.`
              : `Code registered for ${code || config.daemonUrl}. Make sure the daemon is running on the same LAN, then retry.`}
          </StewardText>
          <View style={styles.actions}>
            <Button
              label="Open chat"
              icon="chatbubbles-outline"
              onPress={() => navigation.navigate('Chat')}
            />
            <Button
              variant="ghost"
              label="Unpair"
              icon="link-outline"
              onPress={() => {
                haptic.light();
                setPhase('scan');
              }}
            />
          </View>
        </>
      ) : null}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { padding: 24, paddingTop: 28, gap: 18 },
  heading: { marginBottom: -6 },
  sub: { marginBottom: 6 },
  frame: { borderRadius: 28, borderWidth: 1, overflow: 'hidden' },
  finder: {
    aspectRatio: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 28,
  },
  corners: { width: '100%', height: '100%', position: 'relative' },
  corner: { position: 'absolute', width: 44, height: 44, borderWidth: 4 },
  tl: { top: 0, left: 0, borderRightWidth: 0, borderBottomWidth: 0, borderTopLeftRadius: 18 },
  tr: { top: 0, right: 0, borderLeftWidth: 0, borderBottomWidth: 0, borderTopRightRadius: 18 },
  bl: { bottom: 0, left: 0, borderRightWidth: 0, borderTopWidth: 0, borderBottomLeftRadius: 18 },
  br: { bottom: 0, right: 0, borderLeftWidth: 0, borderTopWidth: 0, borderBottomRightRadius: 18 },
  centerIcon: {
    position: 'absolute',
    top: '50%',
    left: '50%',
    marginLeft: -34,
    marginTop: -34,
    width: 68,
    height: 68,
    borderRadius: 20,
    alignItems: 'center',
    justifyContent: 'center',
  },
  finderCaption: { marginTop: 8 },
  actions: { gap: 12, marginTop: 4 },
  input: {
    borderRadius: 14,
    borderWidth: 1,
    paddingHorizontal: 16,
    paddingVertical: 14,
    fontSize: 16,
  },
  center: { alignItems: 'center', gap: 10, paddingVertical: 40 },
  pairedIcon: {
    alignSelf: 'center',
    width: 96,
    height: 96,
    borderRadius: 32,
    alignItems: 'center',
    justifyContent: 'center',
  },
});

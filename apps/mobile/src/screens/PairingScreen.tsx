import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  TextInput,
  View,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { CameraView, useCameraPermissions } from 'expo-camera';
import type { BottomTabScreenProps } from '@react-navigation/bottom-tabs';
import type { MainTabParamList } from '../navigation/types';
import { useTheme } from '../theme';
import { StewardText } from '../components/StewardText';
import { Card } from '../components/Card';
import { Button } from '../components/Button';
import { haptic } from '../lib/haptics';
import { rnStorage, secureTokenStore } from '../lib/storage';
import { ConversationCache } from '../lib/cache';
import { deviceLabel, SessionManager } from '../lib/session';
import { StewardClient, StewardError } from '../lib/client';
import {
  DEFAULT_DAEMON_PORT,
  daemonUrl,
  findDaemonAtGatewayCandidates,
  normalizeHost,
  parsePairUri,
} from '../lib/pairing';
import { sessionStore } from '../lib/sessionStore';
import { useSession } from '../hooks/useSession';
import type { Session } from '../lib/types';

type Props = BottomTabScreenProps<MainTabParamList, 'Pair'>;

type Phase = 'form' | 'connecting' | 'success' | 'error';

export function PairingScreen({ navigation }: Props) {
  const t = useTheme();
  const c = t.colors;

  const [permission, requestPermission] = useCameraPermissions();
  const session = useSession();

  const sessions = useMemo(() => new SessionManager(rnStorage, secureTokenStore), []);
  const cache = useMemo(() => new ConversationCache(rnStorage), []);

  const [phase, setPhase] = useState<Phase>('form');
  const [host, setHost] = useState('');
  const [port, setPort] = useState(String(DEFAULT_DAEMON_PORT));
  const [code, setCode] = useState('');
  const [device, setDevice] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [probeMessage, setProbeMessage] = useState<string | null>(null);
  const [scanMessage, setScanMessage] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [cameraActive, setCameraActive] = useState(true);
  const [daemonInfo, setDaemonInfo] = useState<{
    version: string;
    backend: string;
    model: string;
  } | null>(null);
  const scanInFlight = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Prefill from the current session and set a stable device label.
  useEffect(() => {
    void deviceLabel(rnStorage, Platform.OS).then((label) => {
      if (mounted.current) setDevice(label);
    });
    if (session) {
      setHost(session.host);
      setPort(String(session.port));
    } else if (!host) {
      setPort(String(DEFAULT_DAEMON_PORT));
    }
  }, [session]); // eslint-disable-line react-hooks/exhaustive-deps

  const attemptPair = useCallback(
    async (hostIn: string, portIn: number, codeIn: string) => {
      const h = normalizeHost(hostIn);
      const codeStr = codeIn.trim();
      if (!h || !/^\d{1,5}$/.test(String(portIn))) {
        setError('Enter the daemon address and port shown on the daemon screen.');
        setPhase('error');
        return;
      }
      if (!/^\d{6}$/.test(codeStr.replace(/\s+/g, ''))) {
        setError('Enter the 6-digit pairing code shown on the daemon screen.');
        setPhase('error');
        return;
      }
      setError(null);
      setPhase('connecting');
      try {
        const freshSession: Session = await StewardClient.pairAndPersist({
          host: h,
          port: portIn,
          code: codeStr.replace(/\s+/g, ''),
          device: device.trim() || 'phone',
          sessions,
        });
        if (!mounted.current) return;
        sessionStore.set(freshSession);
        // Verify the authed handshake and grab About info.
        const client = StewardClient.fromSession(freshSession, cache);
        const health = await client.health();
        if (mounted.current) {
          setDaemonInfo({
            version: health.version,
            backend: health.backend,
            model: health.model,
          });
          setPhase('success');
          haptic.success();
        }
      } catch (err) {
        if (!mounted.current) return;
        if (err instanceof StewardError) {
          if (err.status === 401) {
            setError('Invalid pairing code. Check the 6 digits on the daemon screen.');
          } else if (err.status === 429) {
            setError('Too many failed attempts. Wait a few minutes and retry.');
          } else if (err.kind === 'network') {
            setError(`Could not reach ${daemonUrl(h, portIn)}. Check the address and that the daemon is running.`);
          } else {
            setError(err.message);
          }
        } else {
          setError(err instanceof Error ? err.message : String(err));
        }
        setPhase('error');
        haptic.warning();
      }
    },
    [sessions, cache, device],
  );

  const onQrScanned = useCallback(
    (result: { data: string }) => {
      if (scanInFlight.current || phase === 'connecting') return;
      const uri = result.data;
      const parsed = parsePairUri(uri);
      if (!parsed) {
        setScanMessage('That QR code is not a Steward pairing code. Show the code on the daemon screen.');
        haptic.warning();
        return;
      }
      scanInFlight.current = true;
      setScanMessage(`Scanned daemon at ${parsed.host}:${parsed.port}`);
      haptic.light();
      setHost(parsed.host);
      setPort(String(parsed.port));
      setCode(parsed.code ?? '');
      setError(null);
      void attemptPair(parsed.host, parsed.port, parsed.code ?? '');
      setTimeout(() => {
        scanInFlight.current = false;
      }, 1200);
    },
    [attemptPair, phase],
  );

  const tryGateways = useCallback(async () => {
    if (searching) return;
    setSearching(true);
    setProbeMessage('Probing common gateways for a Steward daemon');
    haptic.light();
    const found = await findDaemonAtGatewayCandidates({ port: Number(port) || DEFAULT_DAEMON_PORT });
    if (!mounted.current) return;
    setSearching(false);
    if (found) {
      const url = new URL(found);
      setHost(url.hostname);
      setPort(url.port || String(DEFAULT_DAEMON_PORT));
      setProbeMessage(`Found a daemon at ${found}. Enter the pairing code from its screen.`);
      haptic.success();
    } else {
      setProbeMessage(
        'No daemon on the common gateways. The daemon usually listens on the main IP of your computer, not the router. Type that address below.',
      );
    }
  }, [searching, port]);

  const unpair = useCallback(async () => {
    await sessions.clear();
    sessionStore.set(null);
    setHost('');
    setDaemonInfo(null);
    haptic.warning();
  }, [sessions]);

  const cameraOn = permission?.granted && cameraActive;

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
        Link to a Steward daemon on your LAN. The connection stays local, end
        to end, on hardware you own. No cloud, no account, no subscription.
      </StewardText>

      {phase === 'success' ? (
        <View style={styles.center}>
          <View style={[styles.pairedIcon, { backgroundColor: c.brandContainer }]}>
            <Ionicons name="checkmark-circle" size={52} color={c.brand} />
          </View>
          <StewardText variant="headline" align="center">
            Paired
          </StewardText>
          <StewardText variant="body" secondary align="center" style={styles.centerBody}>
            Connected to {daemonUrl(host, Number(port))}. Your assistant is ready.
            {daemonInfo ? ` Daemon ${daemonInfo.version}, backend ${daemonInfo.backend}, model ${daemonInfo.model}.` : ''}
          </StewardText>
          <View style={styles.actions}>
            <Button
              label="Open chat"
              icon="chatbubbles-outline"
              onPress={() => navigation.navigate('Chat', { conversationId: undefined })}
            />
            <Button variant="ghost" label="Back to form" icon="arrow-back" onPress={() => setPhase('form')} />
          </View>
        </View>
      ) : (
        <>
          {cameraActive ? (
            <View style={[styles.frame, { borderColor: c.border }]}>
              {cameraOn ? (
                <CameraView
                  style={styles.camera}
                  facing="back"
                  barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
                  onBarcodeScanned={onQrScanned}
                />
              ) : (
                <View style={[styles.cameraPlaceholder, { backgroundColor: c.surface }]}>
                  <Ionicons name="qr-code-outline" size={44} color={c.brand} />
                  <StewardText variant="caption" tertiary align="center" style={styles.cameraHint}>
                    {permission?.granted === false
                      ? 'Camera permission off. Manual entry below still works.'
                      : 'Camera preview'}
                  </StewardText>
                  {permission?.granted === false ? (
                    <Button
                      variant="ghost"
                      label="Enable camera"
                      icon="camera-outline"
                      onPress={() => void requestPermission()}
                    />
                  ) : null}
                </View>
              )}
              <View style={styles.cameraFooter}>
                <Pressable
                  onPress={() => {
                    setCameraActive(false);
                    haptic.light();
                  }}
                  hitSlop={8}
                  style={styles.cameraToggle}
                >
                  <Ionicons name="eye-off-outline" size={16} color={c.textTertiary} />
                  <StewardText variant="caption" tertiary>Hide camera</StewardText>
                </Pressable>
                {scanMessage ? (
                  <StewardText variant="caption" tertiary style={styles.scanMsg} numberOfLines={1}>
                    {scanMessage}
                  </StewardText>
                ) : null}
              </View>
            </View>
          ) : (
            <Button
              variant="ghost"
              label="Show camera"
              icon="camera-outline"
              onPress={() => {
                setCameraActive(true);
                if (permission?.granted === false) void requestPermission();
              }}
            />
          )}

          <Card style={styles.formCard}>
            <StewardText variant="caption" tertiary style={styles.fieldLabel}>
              DAEMON ADDRESS (shown on the daemon screen)
            </StewardText>
            <View style={styles.row}>
              <TextInput
                value={host}
                onChangeText={setHost}
                placeholder="192.168.1.50"
                placeholderTextColor={c.textTertiary}
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType={Platform.OS === 'ios' ? 'url' : 'ascii-capable'}
                style={[
                  styles.input,
                  styles.hostInput,
                  { color: c.textPrimary, backgroundColor: c.surface, borderColor: c.border },
                ]}
              />
              <TextInput
                value={port}
                onChangeText={(p) => setPort(p.replace(/[^0-9]/g, ''))}
                placeholder="3877"
                placeholderTextColor={c.textTertiary}
                keyboardType="number-pad"
                style={[
                  styles.input,
                  styles.portInput,
                  { color: c.textPrimary, backgroundColor: c.surface, borderColor: c.border },
                ]}
              />
            </View>

            <StewardText variant="caption" tertiary style={styles.fieldLabel}>
              PAIRING CODE (6 digits, shown on the daemon screen)
            </StewardText>
            <TextInput
              value={code}
              onChangeText={(v) => setCode(v.replace(/[^0-9]/g, '').slice(0, 6))}
              placeholder="123456"
              placeholderTextColor={c.textTertiary}
              keyboardType="number-pad"
              maxLength={6}
              secureTextEntry={false}
              style={[
                styles.input,
                styles.codeInput,
                { color: c.textPrimary, backgroundColor: c.surface, borderColor: c.border },
              ]}
            />

            <View style={styles.actions}>
              <Button
                label="Pair"
                icon="link-outline"
                onPress={() => attemptPair(host, Number(port || DEFAULT_DAEMON_PORT), code)}
                disabled={phase === 'connecting' || searching}
              />
              <Button
                variant="tonal"
                label={searching ? 'Searching…' : 'Try common gateways'}
                icon="wifi-outline"
                onPress={() => void tryGateways()}
                disabled={phase === 'connecting' || searching}
              />
            </View>
            {searching ? (
              <View style={styles.probeRow}>
                <ActivityIndicator size="small" color={c.brand} />
                <StewardText variant="caption" secondary>{probeMessage}</StewardText>
              </View>
            ) : probeMessage ? (
              <StewardText variant="caption" secondary style={styles.probeNote}>
                {probeMessage}
              </StewardText>
            ) : null}
          </Card>

          <StewardText variant="caption" tertiary style={styles.discoveryNote}>
            Discovery note: phones do not auto-find the daemon over the network.
            Daemons advertise over mDNS, but Expo Go cannot browse mDNS, so
            Steward does not pretend to. Type the address the daemon printed
            (its LAN IP and port). "Try common gateways" only finds a daemon
            that happens to live on one of those addresses; it is a shortcut,
            not a search.
          </StewardText>

          {session ? (
            <View style={styles.unpairWrap}>
              <Button variant="ghost" label="Unpair this phone" icon="link-outline" onPress={() => void unpair()} />
            </View>
          ) : null}
        </>
      )}

      {phase === 'connecting' ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color={c.brand} />
          <StewardText variant="headline" align="center">
            Pairing…
          </StewardText>
          <StewardText variant="body" secondary align="center">
            Exchanging a session token with {daemonUrl(host, Number(port || DEFAULT_DAEMON_PORT))}
          </StewardText>
        </View>
      ) : null}

      {phase === 'error' && error ? (
        <View style={[styles.errorBox, { backgroundColor: c.surface, borderColor: c.danger }]}>
          <Ionicons name="alert-circle-outline" size={18} color={c.danger} />
          <StewardText variant="body" color={c.danger} style={styles.errorText}>
            {error}
          </StewardText>
        </View>
      ) : null}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { padding: 24, paddingTop: 28, gap: 18 },
  heading: { marginBottom: -6 },
  sub: { marginBottom: 6 },
  frame: { borderRadius: 28, borderWidth: 1, overflow: 'hidden' },
  camera: { width: '100%', height: 280 },
  cameraPlaceholder: {
    height: 220,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 10,
    padding: 20,
  },
  cameraHint: { maxWidth: 260, textAlign: 'center' },
  cameraFooter: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 10,
    paddingHorizontal: 14,
    paddingVertical: 10,
    backgroundColor: 'transparent',
  },
  cameraToggle: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  scanMsg: { flex: 1, textAlign: 'right' },
  formCard: { gap: 10 },
  fieldLabel: { marginLeft: 2, marginTop: 2 },
  row: { flexDirection: 'row', gap: 10 },
  hostInput: { flex: 3 },
  portInput: { flex: 1, minWidth: 88 },
  codeInput: { letterSpacing: 8, fontSize: 20, textAlign: 'center' },
  input: {
    borderRadius: 14,
    borderWidth: 1,
    paddingHorizontal: 16,
    paddingVertical: 14,
    fontSize: 16,
  },
  actions: { gap: 12, marginTop: 8 },
  probeRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  probeNote: { lineHeight: 18 },
  discoveryNote: { lineHeight: 17, paddingHorizontal: 2 },
  unpairWrap: { marginTop: 4 },
  center: { alignItems: 'center', gap: 10, paddingVertical: 28 },
  centerBody: { maxWidth: 340, textAlign: 'center' },
  pairedIcon: {
    alignSelf: 'center',
    width: 96,
    height: 96,
    borderRadius: 32,
    alignItems: 'center',
    justifyContent: 'center',
  },
  errorBox: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
    borderRadius: 14,
    borderWidth: 1,
    padding: 14,
  },
  errorText: { flex: 1 },
});

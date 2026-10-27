import { useCallback, useEffect, useMemo, useState } from 'react';
import { ActivityIndicator, ScrollView, StyleSheet, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import type { BottomTabScreenProps } from '@react-navigation/bottom-tabs';
import type { MainTabParamList } from '../navigation/types';
import { useTheme } from '../theme';
import { StewardText } from '../components/StewardText';
import { Card } from '../components/Card';
import { ListRow } from '../components/ListRow';
import { Button } from '../components/Button';
import { haptic } from '../lib/haptics';
import { rnStorage, secureTokenStore } from '../lib/storage';
import { ConversationCache } from '../lib/cache';
import { SessionManager } from '../lib/session';
import { StewardClient, StewardError } from '../lib/client';
import { useSession } from '../hooks/useSession';
import { sessionStore } from '../lib/sessionStore';
import { config } from '../config';
import type { HealthInfo, LicenseReport } from '../lib/types';

type Props = BottomTabScreenProps<MainTabParamList, 'Settings'>;

export function SettingsScreen({ navigation }: Props) {
  const t = useTheme();
  const c = t.colors;
  const session = useSession();

  const cache = useMemo(() => new ConversationCache(rnStorage), []);
  const sessions = useMemo(() => new SessionManager(rnStorage, secureTokenStore), []);
  const client = useMemo(
    () => (session ? StewardClient.fromSession(session, cache) : null),
    [session, cache],
  );

  const [license, setLicense] = useState<LicenseReport | null>(null);
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [licenseLoading, setLicenseLoading] = useState(false);

  const load = useCallback(async () => {
    if (!client) return;
    setLicenseLoading(true);
    try {
      const [licenseReport, healthInfo] = await Promise.all([
        client.license(),
        client.health(),
      ]);
      setLicense(licenseReport);
      setHealth(healthInfo);
    } catch {
      // nothing to set: the cards show the not-paired / offline states
    } finally {
      setLicenseLoading(false);
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  const unpair = useCallback(async () => {
    await sessions.clear();
    sessionStore.set(null);
    setLicense(null);
    setHealth(null);
    haptic.warning();
  }, [sessions]);

  if (!session) {
    return (
      <ScrollView style={{ backgroundColor: c.bg }} contentContainerStyle={styles.content}>
        <Card elevated style={styles.licenseCard}>
          <View style={styles.licenseTop}>
            <View style={[styles.badge, { backgroundColor: c.brandContainer }]}>
              <Ionicons name="infinite-outline" size={15} color={c.brandStrong} />
              <StewardText variant="caption" color={c.brandStrong}>
                PERPETUAL
              </StewardText>
            </View>
          </View>
          <StewardText variant="headline">Steward is yours to own.</StewardText>
          <StewardText variant="body" secondary style={styles.licenseBody}>
            A perpetual license with a single one-time price. Own the software
            outright, get every update, and never face a subscription to
            cancel. No subscription, now or ever.
          </StewardText>
        </Card>

        <View style={styles.section}>
          <Button
            label="Pair a daemon"
            icon="qr-code-outline"
            onPress={() => navigation.navigate('Pair')}
          />
        </View>

        <View style={styles.section}>
          <StewardText variant="caption" tertiary style={styles.sectionLabel}>ABOUT</StewardText>
          <Card>
            <ListRow icon="sparkles-outline" title={config.appName} subtitle={config.appTagline} />
            <ListRow icon="information-circle-outline" title="App version" right={config.version} />
            <ListRow icon="server-outline" title="Daemon" subtitle="Not paired" right="-" />
          </Card>
        </View>

        <StewardText variant="caption" tertiary align="center" style={styles.foot}>
          Steward never runs a subscription. Your data stays on your hardware.
        </StewardText>
      </ScrollView>
    );
  }

  const perpetual = license?.state === 'perpetual';
  const daemonLabel = `${session.host}:${session.port}`;

  return (
    <ScrollView style={{ backgroundColor: c.bg }} contentContainerStyle={styles.content}>
      <Card elevated style={styles.licenseCard}>
        <View style={styles.licenseTop}>
          <View style={[styles.badge, { backgroundColor: c.brandContainer }]}>
            <Ionicons
              name={perpetual ? 'infinite-outline' : 'time-outline'}
              size={15}
              color={c.brandStrong}
            />
            <StewardText variant="caption" color={c.brandStrong}>
              {perpetual ? 'PERPETUAL' : 'FREE EVAL'}
            </StewardText>
          </View>
          {licenseLoading ? (
            <ActivityIndicator size="small" color={c.brand} style={styles.licenseSpinner} />
          ) : null}
        </View>
        <StewardText variant="headline">
          {perpetual
            ? 'You own Steward.'
            : license?.state === 'free-eval'
              ? 'Try Steward free.'
              : 'License status'}
        </StewardText>
        {license?.state === 'perpetual' ? (
          <StewardText variant="body" secondary style={styles.licenseBody}>
            {license.info.label || 'Perpetual license, one-time purchase, all updates included. No subscription now or ever.'}
          </StewardText>
        ) : license?.state === 'free-eval' ? (
          <StewardText variant="body" secondary style={styles.licenseBody}>
            {license.info.label || 'A time-limited evaluation with full features. One-time perpetual upgrade when you are ready. No subscription.'}
          </StewardText>
        ) : license?.state === 'not-served' ? (
          <StewardText variant="body" secondary style={styles.licenseBody}>
            This daemon version does not serve /v1/license yet, so it cannot
            report a tier. Steward is a perpetual-license product: one-time
            price, all updates included, no subscription now or ever.
          </StewardText>
        ) : (
          <StewardText variant="body" secondary style={styles.licenseBody}>
            License status is unavailable right now (daemon unreachable or not
            reporting). Steward is a perpetual-license product: no
            subscription now or ever.
          </StewardText>
        )}
        <StewardText variant="label" color={c.brand}>
          One-time price, no subscription
        </StewardText>
      </Card>

      <View style={styles.section}>
        <StewardText variant="caption" tertiary style={styles.sectionLabel}>CONNECTION</StewardText>
        <Card>
          <ListRow
            icon="qr-code-outline"
            title="Pair a daemon"
            subtitle={daemonLabel}
            right="paired"
            onPress={() => navigation.navigate('Pair')}
          />
          <ListRow
            icon="server-outline"
            title="Daemon address"
            subtitle={daemonLabel}
            right={health?.ok === false ? 'offline' : 'LAN'}
          />
          <ListRow
            icon="shield-checkmark-outline"
            title="Local only"
            subtitle="No cloud relay, ever"
          />
          <ListRow
            icon="link-outline"
            title="Unpair this phone"
            subtitle="Forget the token stored on this device"
            destructive
            onPress={() => void unpair()}
          />
        </Card>
      </View>

      <View style={styles.section}>
        <StewardText variant="caption" tertiary style={styles.sectionLabel}>ABOUT</StewardText>
        <Card>
          <ListRow icon="sparkles-outline" title={config.appName} subtitle={config.appTagline} />
          <ListRow icon="information-circle-outline" title="App version" right={config.version} />
          <ListRow
            icon="server-outline"
            title="Daemon version"
            right={health?.version ?? 'not reported'}
          />
          <ListRow
            icon="cube-outline"
            title="Backend"
            subtitle={health?.backend ?? 'not reported'}
            right={health?.modelResident ? 'model resident' : undefined}
          />
          <ListRow
            icon="layers-outline"
            title="Model"
            subtitle="Also shown in chat"
            right={health?.model ?? 'not reported'}
          />
        </Card>
      </View>

      <StewardText variant="caption" tertiary align="center" style={styles.foot}>
        Steward never runs a subscription. Your data stays on your hardware.
      </StewardText>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { padding: 24, paddingTop: 20, paddingBottom: 48, gap: 22 },
  licenseCard: { gap: 10 },
  licenseTop: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  licenseSpinner: { marginRight: 2 },
  badge: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 999,
  },
  licenseBody: { marginTop: -2 },
  section: { gap: 10 },
  sectionLabel: { marginLeft: 4 },
  foot: { marginTop: 8 },
});

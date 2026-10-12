import { useState } from 'react';
import { Modal, Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import type { BottomTabScreenProps } from '@react-navigation/bottom-tabs';
import type { MainTabParamList } from '../navigation/types';
import { useTheme } from '../theme';
import { StewardText } from '../components/StewardText';
import { Card } from '../components/Card';
import { ListRow } from '../components/ListRow';
import { Button } from '../components/Button';
import { haptic } from '../lib/haptics';
import { config, LicenseTier } from '../config';

type Props = BottomTabScreenProps<MainTabParamList, 'Settings'>;

const PERPETUAL_PRICE = 'One-time $79 · $0 per month · no subscription';

export function SettingsScreen({ navigation }: Props) {
  const t = useTheme();
  const c = t.colors;
  const [tier, setTier] = useState<LicenseTier>('free-eval');
  const [upgradeOpen, setUpgradeOpen] = useState(false);
  const perpetual = tier === 'perpetual';

  const unlockPerpetual = () => {
    setTier('perpetual');
    setUpgradeOpen(false);
    haptic.success();
  };

  return (
    <ScrollView
      style={{ backgroundColor: c.bg }}
      contentContainerStyle={styles.content}
    >
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
        </View>
        <StewardText variant="headline">
          {perpetual ? 'You own Steward.' : 'Try Steward free.'}
        </StewardText>
        <StewardText variant="body" secondary style={styles.licenseBody}>
          {perpetual
            ? 'Perpetual license, one-time purchase, all updates included. No subscription now or ever.'
            : 'A time-limited evaluation with full features. Unlock the perpetual license once, keep it forever.'}
        </StewardText>
        <StewardText variant="label" color={c.brand}>
          {PERPETUAL_PRICE}
        </StewardText>
      </Card>

      <View style={styles.section}>
        {!perpetual ? (
          <Button
            label="Unlock perpetual license"
            icon="key-outline"
            onPress={() => {
              haptic.medium();
              setUpgradeOpen(true);
            }}
          />
        ) : (
          <Button
            label="License active"
            icon="checkmark-circle-outline"
            disabled
          />
        )}
      </View>

      <View style={styles.section}>
        <StewardText variant="caption" tertiary style={styles.sectionLabel}>
          CONNECTION
        </StewardText>
        <Card>
          <ListRow
            icon="qr-code-outline"
            title="Pair a daemon"
            subtitle="Scan or enter the pairing code"
            right="LAN"
            onPress={() => navigation.navigate('Pair')}
          />
          <ListRow
            icon="server-outline"
            title="Daemon address"
            subtitle={config.daemonUrl}
            right="env"
          />
          <ListRow
            icon="shield-checkmark-outline"
            title="Local only"
            subtitle="No cloud relay, ever"
          />
        </Card>
      </View>

      <View style={styles.section}>
        <StewardText variant="caption" tertiary style={styles.sectionLabel}>
          ABOUT
        </StewardText>
        <Card>
          <ListRow icon="sparkles-outline" title={config.appName} subtitle={config.appTagline} />
          <ListRow icon="information-circle-outline" title="Version" right={config.version} />
          <ListRow
            icon="document-text-outline"
            title="License"
            subtitle={perpetual ? 'Perpetual, one-time' : 'Free evaluation'}
            right={perpetual ? 'Active' : 'Eval'}
          />
        </Card>
      </View>

      <StewardText variant="caption" tertiary align="center" style={styles.foot}>
        Steward never runs a subscription. Your data stays on your hardware.
      </StewardText>

      <Modal
        visible={upgradeOpen}
        transparent
        animationType="fade"
        onRequestClose={() => setUpgradeOpen(false)}
      >
        <Pressable style={styles.backdrop} onPress={() => setUpgradeOpen(false)}>
          <Pressable onPress={() => {}}>
            <Card elevated style={styles.modal}>
              <StewardText variant="headline">Unlock perpetual</StewardText>
              <StewardText variant="body" secondary style={styles.licenseBody}>
                One-time purchase, owns the software forever. All future updates
                included. Stub upgrade path: the payment step connects in a
                later sprint. Tapping unlock activates the perpetual state.
              </StewardText>
              <View style={styles.modalActions}>
                <Button label="Unlock (stub)" icon="key-outline" onPress={unlockPerpetual} />
                <Button
                  variant="ghost"
                  label="Not now"
                  onPress={() => setUpgradeOpen(false)}
                />
              </View>
            </Card>
          </Pressable>
        </Pressable>
      </Modal>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { padding: 24, paddingTop: 20, paddingBottom: 48, gap: 22 },
  licenseCard: { gap: 10 },
  licenseTop: { flexDirection: 'row', justifyContent: 'flex-start' },
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
  backdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.45)',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 28,
  },
  modal: { width: '100%', gap: 12 },
  modalActions: { gap: 10, marginTop: 6 },
});

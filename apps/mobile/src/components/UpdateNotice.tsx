import { useState } from 'react';
import { ActivityIndicator, StyleSheet, View } from 'react-native';
import { useTheme } from '../theme';
import { StewardText } from './StewardText';
import { Card } from './Card';
import { Button } from './Button';
import { dismissUpdate } from '../lib/updatesStore';
import type { StorageLike } from '../lib/cache';
import type { MobileUpdatePlan } from '../lib/types';

type Props = {
  plan: MobileUpdatePlan;
  storage: StorageLike;
  onInstall: () => void;
  installing?: boolean;
};

/**
 * Non-modal notice for a newer mobile app build. Shown at the top of Chat
 * when the foreground check found an installable APK; dismissed per version
 * (stored), re-checks on the next foreground. Never blocks the screen behind
 * it.
 */
export function UpdateNotice({ plan, storage, onInstall, installing }: Props) {
  const t = useTheme();
  const c = t.colors;
  const [hidden, setHidden] = useState(false);

  if (hidden || !plan.available || installing) {
    return null;
  }
  const notes = plan.notesUrl ?? plan.notes;

  return (
    <Card elevated style={styles.card}>
      <View style={styles.row}>
        <View style={[styles.badge, { backgroundColor: c.brandContainer }]}>
          <StewardText variant="caption" color={c.brandStrong}>
            UPDATE
          </StewardText>
        </View>
        {installing ? (
          <ActivityIndicator size="small" color={c.brand} />
        ) : null}
      </View>
      <StewardText variant="headline">
        Steward {plan.toVersion} is ready to install
      </StewardText>
      <StewardText variant="caption" secondary>
        {plan.fromVersion} on this phone
      </StewardText>
      {notes ? (
        <StewardText variant="caption" secondary numberOfLines={3}>
          What is new: {plan.notes ?? ''}
        </StewardText>
      ) : null}
      <View style={styles.actions}>
        <Button label="Install now" icon="download-outline" onPress={onInstall} />
        <Button
          label="Not now"
          variant="ghost"
          onPress={() => {
            void dismissUpdate(storage, plan.toVersion);
            setHidden(true);
          }}
        />
      </View>
    </Card>
  );
}

const styles = StyleSheet.create({
  card: { gap: 8, marginBottom: 2 },
  row: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  badge: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 999,
  },
  actions: { flexDirection: 'row', gap: 10, marginTop: 4 },
});

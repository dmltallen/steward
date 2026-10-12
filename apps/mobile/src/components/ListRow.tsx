import { Pressable, StyleSheet, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { useTheme } from '../theme';
import { StewardText } from './StewardText';
import { haptic } from '../lib/haptics';

type Props = {
  icon: keyof typeof Ionicons.glyphMap;
  iconColor?: string;
  title: string;
  subtitle?: string;
  right?: string;
  onPress?: () => void;
  destructive?: boolean;
};

/** Native-feeling row: icon tile, title, subtitle, optional value and chevron. */
export function ListRow({
  icon,
  iconColor,
  title,
  subtitle,
  right,
  onPress,
  destructive,
}: Props) {
  const t = useTheme();
  const c = t.colors;
  const fg = destructive ? c.danger : iconColor ?? c.brand;

  const row = (
    <View style={[styles.row, { borderBottomColor: c.separator }]}>
      <View style={[styles.tile, { backgroundColor: c.brandContainer }]}>
        <Ionicons name={icon} size={19} color={fg} />
      </View>
      <View style={styles.texts}>
        <StewardText variant="label">{title}</StewardText>
        {subtitle ? (
          <StewardText variant="caption" secondary>
            {subtitle}
          </StewardText>
        ) : null}
      </View>
      {right ? (
        <StewardText variant="caption" tertiary style={styles.right}>
          {right}
        </StewardText>
      ) : null}
      {onPress ? (
        <Ionicons
          name="chevron-forward"
          size={18}
          color={c.textTertiary}
          style={styles.chevron}
        />
      ) : null}
    </View>
  );

  if (!onPress) return row;
  return (
    <Pressable
      onPress={() => {
        haptic.light();
        onPress();
      }}
      style={({ pressed }) => ({ opacity: pressed ? 0.6 : 1 })}
    >
      {row}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: 14,
    borderBottomWidth: StyleSheet.hairlineWidth,
    gap: 12,
  },
  tile: {
    width: 36,
    height: 36,
    borderRadius: 11,
    alignItems: 'center',
    justifyContent: 'center',
  },
  texts: { flex: 1, gap: 2 },
  right: { maxWidth: 120, textAlign: 'right' },
  chevron: { marginLeft: 2 },
});

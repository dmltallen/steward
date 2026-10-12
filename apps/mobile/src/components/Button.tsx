import { Pressable, StyleSheet, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { useTheme } from '../theme';
import { StewardText } from './StewardText';
import { haptic } from '../lib/haptics';

type Props = {
  label: string;
  onPress?: () => void;
  variant?: 'primary' | 'tonal' | 'ghost';
  disabled?: boolean;
  icon?: keyof typeof Ionicons.glyphMap;
};

/** Large, calm press targets with haptic feedback; the app's only button affordance. */
export function Button({ label, onPress, variant = 'primary', disabled, icon }: Props) {
  const t = useTheme();
  const c = t.colors;

  const bg =
    variant === 'primary'
      ? c.brand
      : variant === 'tonal'
        ? c.brandContainer
        : 'transparent';
  const fg =
    variant === 'primary' ? c.onBrand : variant === 'tonal' ? c.brandStrong : c.brand;
  const bordered = variant === 'ghost';

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      disabled={disabled}
      onPress={() => {
        haptic.light();
        onPress?.();
      }}
      style={({ pressed }) => [
        styles.base,
        {
          backgroundColor: bg,
          borderColor: c.border,
          borderWidth: bordered ? 1 : 0,
          opacity: disabled ? 0.45 : pressed ? 0.86 : 1,
          transform: [{ scale: pressed ? 0.985 : 1 }],
        },
      ]}
    >
      {icon ? <Ionicons name={icon} size={20} color={fg} /> : null}
      <StewardText style={styles.label} color={fg} variant="label">
        {label}
      </StewardText>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  base: {
    minHeight: 56,
    borderRadius: 999,
    paddingHorizontal: 24,
    alignItems: 'center',
    justifyContent: 'center',
    flexDirection: 'row',
    gap: 8,
  },
  label: { letterSpacing: 0.2 },
});

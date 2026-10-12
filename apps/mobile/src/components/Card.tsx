import {
  Pressable,
  StyleSheet,
  View,
  StyleProp,
  ViewStyle,
} from 'react-native';
import type { ReactNode } from 'react';
import { useTheme } from '../theme';

type Props = {
  children: ReactNode;
  onPress?: () => void;
  padding?: number;
  style?: StyleProp<ViewStyle>;
  elevated?: boolean;
};

/** Surface card with a hairline border; the base container for content blocks. */
export function Card({ children, onPress, padding, style, elevated }: Props) {
  const t = useTheme();
  const c = t.colors;
  const inner = (
    <View
      style={[
        styles.card,
        {
          backgroundColor: c.surface,
          borderColor: c.border,
          padding: padding ?? t.spacing.lg,
          borderWidth: elevated ? 0 : 1,
          ...(elevated
            ? {
                shadowColor: '#000',
                shadowOpacity: t.isDark ? 0.4 : 0.08,
                shadowRadius: 18,
                shadowOffset: { width: 0, height: 8 },
                elevation: 4,
              }
            : {}),
        },
        style,
      ]}
    >
      {children}
    </View>
  );

  if (!onPress) return inner;
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
    >
      {inner}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  card: { borderRadius: 20, overflow: 'hidden' },
});

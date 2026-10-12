import { StyleSheet, View } from 'react-native';
import { useTheme } from '../theme';

type Props = { count: number; active: number };

/** Onboarding pager dots: the active dot stretches into a calm pill. */
export function ProgressDots({ count, active }: Props) {
  const t = useTheme();
  const c = t.colors;
  return (
    <View style={styles.row}>
      {Array.from({ length: count }).map((_, i) => (
        <View
          key={i}
          style={[
            styles.dot,
            {
              backgroundColor: i === active ? c.brand : 'transparent',
              borderColor: c.border,
              borderWidth: i === active ? 0 : 1,
              width: i === active ? 26 : 8,
            },
          ]}
        />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: 7, height: 10 },
  dot: { height: 8, borderRadius: 4 },
});

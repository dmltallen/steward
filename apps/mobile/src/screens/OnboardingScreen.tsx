import { StyleSheet } from 'react-native';
import { useWindowDimensions } from 'react-native';
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import { Ionicons } from '@expo/vector-icons';
import { LinearGradient } from 'expo-linear-gradient';
import { FlatList, View } from 'react-native';
import { useState } from 'react';
import type { RootStackParamList } from '../navigation/types';
import { useTheme } from '../theme';
import { Button } from '../components/Button';
import { ProgressDots } from '../components/ProgressDots';
import { StewardText } from '../components/StewardText';
import { haptic } from '../lib/haptics';

type Slide = {
  icon: keyof typeof Ionicons.glyphMap;
  eyebrow: string;
  title: string;
  body: string;
  rest: string;
};

const SLIDES: Slide[] = [
  {
    icon: 'leaf-outline',
    eyebrow: 'Intro',
    title: 'The intelligence you own',
    body: 'Steward is a private AI assistant that runs on hardware you already have. Not on a rented server, not in someone elses cloud: on your machines, under your control.',
    rest: 'Truly yours, from day one.',
  },
  {
    icon: 'shield-checkmark-outline',
    eyebrow: 'Privacy',
    title: 'Nothing leaves your network',
    body: 'Your conversations stay on your devices. No cloud dependency, no data harvesting, no account you have to keep paying to keep.',
    rest: 'Your privacy is the product.',
  },
  {
    icon: 'key-outline',
    eyebrow: 'Ownership',
    title: 'One price, forever',
    body: 'A perpetual license with a single one-time payment. Own the software outright, get every update, and never face another subscription to cancel.',
    rest: 'No subscription. Ever.',
  },
];

type Props = NativeStackScreenProps<RootStackParamList, 'Onboarding'>;

export function OnboardingScreen({ navigation }: Props) {
  const t = useTheme();
  const c = t.colors;
  const { width } = useWindowDimensions();
  const [index, setIndex] = useState(0);
  const isLast = index === SLIDES.length - 1;

  return (
    <View style={[styles.screen, { backgroundColor: c.bg }]}>
      <FlatList
        data={SLIDES}
        horizontal
        pagingEnabled
        showsHorizontalScrollIndicator={false}
        keyExtractor={(_, i) => String(i)}
        onMomentumScrollEnd={(e) => {
          const i = Math.round(e.nativeEvent.contentOffset.x / width);
          if (i !== index) {
            setIndex(i);
            haptic.light();
          }
        }}
        renderItem={({ item }) => (
          <View style={[styles.slide, { width }]}>
            <LinearGradient
              colors={[c.brandContainer, c.bg] as const}
              start={{ x: 0.25, y: 0 }}
              end={{ x: 0.85, y: 1 }}
              style={[styles.hero, { borderColor: c.border }]}
            >
              <View style={[styles.heroIcon, { backgroundColor: c.surface }]}>
                <Ionicons name={item.icon} size={42} color={c.brand} />
              </View>
            </LinearGradient>
            <View style={styles.copy}>
              <StewardText variant="label" color={c.brand}>
                {item.eyebrow.toUpperCase()}
              </StewardText>
              <StewardText variant="display" style={styles.title}>
                {item.title}
              </StewardText>
              <StewardText variant="body" secondary style={styles.body}>
                {item.body}
              </StewardText>
              <StewardText variant="label" color={c.brandStrong}>
                {item.rest}
              </StewardText>
            </View>
          </View>
        )}
      />
      <View style={[styles.footer, { borderTopColor: c.separator }]}>
        <ProgressDots count={SLIDES.length} active={index} />
        <View style={styles.actions}>
          {!isLast ? (
            <View style={styles.skipWrap}>
              <Button
                variant="ghost"
                label="Skip"
                onPress={() => {
                  haptic.light();
                  navigation.replace('Main');
                }}
              />
            </View>
          ) : (
            <View style={styles.skipWrap} />
          )}
          <View style={styles.nextWrap}>
            <Button
              label={isLast ? 'Get started' : 'Next'}
              icon={isLast ? 'sparkles-outline' : 'arrow-forward'}
              onPress={() => {
                if (isLast) {
                  haptic.success();
                  navigation.replace('Main');
                } else {
                  setIndex(index + 1);
                }
              }}
            />
          </View>
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1 },
  slide: { flex: 1, paddingBottom: 12 },
  hero: {
    flex: 1,
    marginHorizontal: 24,
    marginTop: 24,
    borderRadius: 28,
    borderWidth: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
  heroIcon: {
    width: 96,
    height: 96,
    borderRadius: 32,
    alignItems: 'center',
    justifyContent: 'center',
    shadowColor: '#000',
    shadowOpacity: 0.1,
    shadowRadius: 16,
    shadowOffset: { width: 0, height: 6 },
    elevation: 3,
  },
  copy: { paddingHorizontal: 28, paddingTop: 28, gap: 10 },
  title: { marginTop: 2 },
  body: { marginTop: 2 },
  footer: {
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 24,
    paddingTop: 18,
    paddingBottom: 16,
    gap: 18,
  },
  actions: { flexDirection: 'row', gap: 12 },
  skipWrap: { flex: 1 },
  nextWrap: { flex: 2 },
});

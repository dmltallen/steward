import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { Ionicons } from '@expo/vector-icons';
import { navigationTheme, useTheme } from '../theme';
import { OnboardingScreen } from '../screens/OnboardingScreen';
import { ChatScreen } from '../screens/ChatScreen';
import { PairingScreen } from '../screens/PairingScreen';
import { SettingsScreen } from '../screens/SettingsScreen';
import type { MainTabParamList, RootStackParamList } from './types';

const Stack = createNativeStackNavigator<RootStackParamList>();
const Tab = createBottomTabNavigator<MainTabParamList>();

const TAB_ICONS: Record<
  keyof MainTabParamList,
  { on: keyof typeof Ionicons.glyphMap; off: keyof typeof Ionicons.glyphMap }
> = {
  Chat: { on: 'chatbubbles', off: 'chatbubbles-outline' },
  Pair: { on: 'qr-code', off: 'qr-code-outline' },
  Settings: { on: 'settings', off: 'settings-outline' },
};

function MainTabs() {
  const t = useTheme();
  const c = t.colors;
  return (
    <Tab.Navigator
      screenOptions={({ route }) => ({
        headerShown: true,
        tabBarActiveTintColor: c.brand,
        tabBarInactiveTintColor: c.textTertiary,
        tabBarStyle: { backgroundColor: c.surface, borderTopColor: c.separator },
        headerStyle: { backgroundColor: c.surface },
        headerTintColor: c.textPrimary,
        headerTitleStyle: { fontWeight: '600' },
        headerShadowVisible: false,
        tabBarIcon: ({ color, size, focused }) => {
          const name = TAB_ICONS[route.name][focused ? 'on' : 'off'];
          return <Ionicons name={name} size={size} color={color} />;
        },
      })}
    >
      <Tab.Screen name="Chat" component={ChatScreen} options={{ title: 'Chat' }} />
      <Tab.Screen
        name="Pair"
        component={PairingScreen}
        options={{ title: 'Pair', headerShown: false }}
      />
      <Tab.Screen name="Settings" component={SettingsScreen} options={{ title: 'Settings' }} />
    </Tab.Navigator>
  );
}

export function RootNavigator() {
  const t = useTheme();
  return (
    <NavigationContainer theme={navigationTheme(t.colors, t.isDark)}>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        <Stack.Screen name="Onboarding" component={OnboardingScreen} />
        <Stack.Screen name="Main" component={MainTabs} />
      </Stack.Navigator>
    </NavigationContainer>
  );
}

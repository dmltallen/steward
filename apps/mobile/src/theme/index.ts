import { useColorScheme } from 'react-native';
import {
  darkColors,
  fontSize,
  fontWeights,
  lightColors,
  radii,
  spacing,
  typography,
} from './tokens';
import type { Palette } from './tokens';
import {
  DarkTheme as NavDark,
  DefaultTheme as NavDefault,
} from '@react-navigation/native';
import type { Theme as NavTheme } from '@react-navigation/native';

export type { Palette, TextVariant } from './tokens';
export { lightColors, darkColors, radii, spacing, typography, fontWeights, fontSize };

export type AppTheme = {
  colors: Palette;
  isDark: boolean;
  spacing: typeof spacing;
  radii: typeof radii;
  typography: typeof typography;
};

/** App-wide theme derived once from the OS color scheme. */
export function useTheme(): AppTheme {
  const scheme = useColorScheme();
  const isDark = scheme === 'dark';
  return {
    colors: isDark ? darkColors : lightColors,
    isDark,
    spacing,
    radii,
    typography,
  };
}

/** Maps the token palette onto React Navigation's theme so headers and tabs follow it. */
export function navigationTheme(colors: Palette, isDark: boolean): NavTheme {
  const base = isDark ? NavDark : NavDefault;
  return {
    ...base,
    dark: isDark,
    colors: {
      ...base.colors,
      primary: colors.brand,
      background: colors.bg,
      card: colors.surface,
      text: colors.textPrimary,
      border: colors.border,
      notification: colors.brand,
    },
  };
}

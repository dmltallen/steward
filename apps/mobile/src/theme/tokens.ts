/**
 * Steward design tokens. Single source of truth for color, typography,
 * spacing and corner radius. Light and dark palettes. Nothing else in the
 * app hard-codes a color, metric or radius: component code reads these.
 */

export type Palette = {
  bg: string;
  surface: string;
  surfaceElevated: string;
  textPrimary: string;
  textSecondary: string;
  textTertiary: string;
  separator: string;
  border: string;
  brand: string;
  brandStrong: string;
  brandContainer: string;
  onBrand: string;
  accent: string;
  success: string;
  warning: string;
  danger: string;
  scrim: string;
};

/** Calm, premium, privacy-first. Deep teal ink on warm paper. */
export const lightColors: Palette = {
  bg: '#FAFAF8',
  surface: '#FFFFFF',
  surfaceElevated: '#FFFFFF',
  textPrimary: '#15211F',
  textSecondary: '#53605C',
  textTertiary: '#7C8A85',
  separator: '#E9EEEB',
  border: '#DCE4E0',
  brand: '#0E6B5A',
  brandStrong: '#0A5447',
  brandContainer: '#DDEDE6',
  onBrand: '#FFFFFF',
  accent: '#B4854A',
  success: '#2F7D4F',
  warning: '#B57717',
  danger: '#B5402C',
  scrim: 'rgba(15, 21, 19, 0.38)',
};

/** Same hue family flipped for low-light rooms. */
export const darkColors: Palette = {
  bg: '#0E1413',
  surface: '#151D1B',
  surfaceElevated: '#1B2523',
  textPrimary: '#ECF3F0',
  textSecondary: '#A8B6B1',
  textTertiary: '#788883',
  separator: '#22302C',
  border: '#2C3A36',
  brand: '#54D6B4',
  brandStrong: '#8CE7CF',
  brandContainer: '#12352C',
  onBrand: '#06201A',
  accent: '#D9A25F',
  success: '#67C08A',
  warning: '#D9A25F',
  danger: '#E07A64',
  scrim: 'rgba(0, 0, 0, 0.45)',
};

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 20,
  xxl: 24,
  '3xl': 32,
  '4xl': 40,
  '5xl': 56,
} as const;

export const radii = {
  sm: 10,
  md: 14,
  lg: 20,
  xl: 28,
  pill: 999,
} as const;

/** Raw font sizes, handy for one-off inline styles. */
export const fontSize = {
  xs: 13,
  sm: 15,
  md: 17,
  lg: 19,
  xl: 24,
  xxl: 30,
  display: 38,
} as const;

export type TextVariant =
  | 'display'
  | 'title'
  | 'headline'
  | 'body'
  | 'label'
  | 'caption';

/** Large, readable type throughout: the brand runs on legibility. */
export const typography: Record<
  TextVariant,
  { size: number; weight: 'regular' | 'medium' | 'semibold' | 'bold'; lineHeight: number; letterSpacing?: number }
> = {
  display: { size: 38, weight: 'bold', lineHeight: 45, letterSpacing: -0.6 },
  title: { size: 30, weight: 'bold', lineHeight: 37, letterSpacing: -0.3 },
  headline: { size: 24, weight: 'semibold', lineHeight: 31, letterSpacing: -0.2 },
  body: { size: 17, weight: 'regular', lineHeight: 25 },
  label: { size: 15, weight: 'semibold', lineHeight: 21 },
  caption: { size: 13, weight: 'regular', lineHeight: 18 },
};

export const fontWeights: Record<
  'regular' | 'medium' | 'semibold' | 'bold',
  '400' | '500' | '600' | '700'
> = {
  regular: '400',
  medium: '500',
  semibold: '600',
  bold: '700',
};

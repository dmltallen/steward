import { StyleSheet, Text, TextProps, TextStyle } from 'react-native';
import { TextVariant, fontWeights, useTheme } from '../theme';

type Props = TextProps & {
  variant?: TextVariant;
  color?: string;
  align?: TextStyle['textAlign'];
  secondary?: boolean;
  tertiary?: boolean;
};

/** Typed text primitives that follow the design tokens. Default color is textPrimary. */
export function StewardText({
  variant = 'body',
  color,
  align,
  secondary,
  tertiary,
  style,
  ...rest
}: Props) {
  const t = useTheme();
  const spec = t.typography[variant];
  const resolved =
    color ??
    (tertiary ? t.colors.textTertiary : secondary ? t.colors.textSecondary : t.colors.textPrimary);
  return (
    <Text
      {...rest}
      style={[
        {
          fontSize: spec.size,
          lineHeight: spec.lineHeight,
          letterSpacing: spec.letterSpacing,
          fontWeight: fontWeights[spec.weight],
          color: resolved,
          textAlign: align,
        },
        style,
      ]}
    />
  );
}

export const textStyles = StyleSheet.create({});

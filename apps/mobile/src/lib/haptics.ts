import * as Haptics from 'expo-haptics';

/**
 * Thin wrapper over expo-haptics so screens get tactile feedback without
 * scattering try/catch everywhere. All calls are best-effort and swallow
 * errors (haptics are a nicety, never a hard dependency).
 */
export const haptic = {
  light: () => {
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
  },
  medium: () => {
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium).catch(() => {});
  },
  success: () => {
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => {});
  },
  warning: () => {
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning).catch(() => {});
  },
};

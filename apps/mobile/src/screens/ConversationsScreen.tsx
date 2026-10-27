import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Pressable,
  StyleSheet,
  View,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import type { BottomTabScreenProps } from '@react-navigation/bottom-tabs';
import type { MainTabParamList } from '../navigation/types';
import { useTheme } from '../theme';
import { StewardText } from '../components/StewardText';
import { useSession } from '../hooks/useSession';
import { rnStorage } from '../lib/storage';
import { ConversationCache } from '../lib/cache';
import { StewardClient, StewardError } from '../lib/client';
import { haptic } from '../lib/haptics';
import type { ConversationSummary } from '../lib/types';

type Props = BottomTabScreenProps<MainTabParamList, 'Conversations'>;

function formatWhen(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export function ConversationsScreen({ navigation }: Props) {
  const t = useTheme();
  const c = t.colors;
  const session = useSession();

  const cache = useMemo(() => new ConversationCache(rnStorage), []);
  const client = useMemo(
    () => (session ? StewardClient.fromSession(session, cache) : null),
    [session, cache],
  );

  const [summaries, setSummaries] = useState<ConversationSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [fromCache, setFromCache] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!client) {
      setSummaries([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const { summaries: result, fromCache: fallback } = await client.listConversations();
      setSummaries(result);
      setFromCache(fallback);
    } catch (err) {
      if (err instanceof StewardError && err.status === 401) {
        setError('Daemon rejected the session token. Re-pair in Settings.');
      } else {
        setError('Daemon unreachable. Showing cached conversations.');
        setFromCache(true);
      }
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
    // Refresh when the subject changes (tab focus or session pairing).
    const unsubscribe = navigation.addListener('focus', refresh);
    return unsubscribe;
  }, [refresh, navigation]);

  const openConversation = useCallback(
    (id: number) => {
      navigation.navigate('Chat', { conversationId: id });
    },
    [navigation],
  );

  const remove = useCallback(
    (item: ConversationSummary) => {
      if (!client) return;
      Alert.alert(
        'Delete conversation',
        `Delete "${item.title}" and its messages from this daemon?`,
        [
          { text: 'Cancel', style: 'cancel' },
          {
            text: 'Delete',
            style: 'destructive',
            onPress: () => {
              void (async () => {
                try {
                  await client.deleteConversation(item.id);
                  haptic.warning();
                  setSummaries((prev) => prev.filter((s) => s.id !== item.id));
                } catch (err) {
                  haptic.warning();
                  setError(err instanceof Error ? err.message : 'Delete failed');
                }
              })();
            },
          },
        ],
        { cancelable: true },
      );
    },
    [client],
  );

  if (!session) {
    return (
      <View style={[styles.screen, styles.center, { backgroundColor: c.bg }]}>
        <StewardText variant="headline" align="center">No daemon paired</StewardText>
        <StewardText variant="body" secondary align="center" style={styles.centerBody}>
          Pair a daemon to see your past conversations here and on this phone
          even when the network is down.
        </StewardText>
        <View style={styles.centerActions}>
          <Pressable
            onPress={() => {
              haptic.light();
              navigation.navigate('Pair');
            }}
            style={[styles.cta, { backgroundColor: c.brand }]}
          >
            <StewardText variant="label" color={c.onBrand}>Pair a daemon</StewardText>
          </Pressable>
        </View>
      </View>
    );
  }

  return (
    <View style={[styles.screen, { backgroundColor: c.bg }]}>
      <View style={[styles.headerRow, { borderBottomColor: c.separator }]}>
        <Pressable
          onPress={() => navigation.navigate('Chat', { conversationId: null })}
          style={[styles.newChat, { backgroundColor: c.brand }]}
          accessibilityRole="button"
          accessibilityLabel="New conversation"
        >
          <Ionicons name="add" size={20} color={c.onBrand} />
          <StewardText variant="label" color={c.onBrand}>New chat</StewardText>
        </Pressable>
      </View>

      {fromCache ? (
        <View style={[styles.offlineBanner, { backgroundColor: c.brandContainer }]}>
          <Ionicons name="cloud-offline-outline" size={14} color={c.brandStrong} />
          <StewardText variant="caption" color={c.brandStrong}>
            Offline: showing cached conversations
          </StewardText>
        </View>
      ) : null}

      {error ? (
        <View style={[styles.errorBanner, { backgroundColor: c.surface, borderColor: c.border }]}>
          <StewardText variant="caption" secondary>{error}</StewardText>
        </View>
      ) : null}

      {loading ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color={c.brand} />
        </View>
      ) : summaries.length === 0 ? (
        <View style={[styles.center, { backgroundColor: c.bg }]}>
          <View style={[styles.emptyIcon, { backgroundColor: c.brandContainer }]}>
            <Ionicons name="chatbubbles-outline" size={40} color={c.brand} />
          </View>
          <StewardText variant="headline" align="center">No conversations yet</StewardText>
          <StewardText variant="body" secondary align="center" style={styles.centerBody}>
            Start one from the Chat tab. Your last 20 messages per conversation
            are cached on this phone for offline reading.
          </StewardText>
        </View>
      ) : (
        <FlatList
          data={summaries}
          keyExtractor={(s) => String(s.id)}
          contentContainerStyle={styles.list}
          ListHeaderComponent={
            <StewardText variant="caption" tertiary style={styles.listLabel}>
              {fromCache ? 'CACHED ON THIS PHONE' : 'ON YOUR DAEMON'}
            </StewardText>
          }
          renderItem={({ item }) => (
            <Pressable
              onPress={() => openConversation(item.id)}
              style={({ pressed }) => [
                styles.row,
                { backgroundColor: c.surface, borderColor: c.border, opacity: pressed ? 0.7 : 1 },
              ]}
              accessibilityRole="button"
              accessibilityLabel={`Open ${item.title}`}
            >
              <View style={[styles.rowIcon, { backgroundColor: c.brandContainer }]}>
                <Ionicons name="chatbubble-ellipses-outline" size={18} color={c.brand} />
              </View>
              <View style={styles.rowTexts}>
                <StewardText variant="label" numberOfLines={1}>{item.title}</StewardText>
                <StewardText variant="caption" secondary numberOfLines={1}>
                  {item.message_count} message{item.message_count === 1 ? '' : 's'}
                </StewardText>
              </View>
              <StewardText variant="caption" tertiary>{formatWhen(item.created_at)}</StewardText>
              <Pressable
                onPress={() => remove(item)}
                hitSlop={10}
                style={styles.delBtn}
                accessibilityRole="button"
                accessibilityLabel={`Delete ${item.title}`}
              >
                <Ionicons name="trash-outline" size={17} color={c.textTertiary} />
              </Pressable>
            </Pressable>
          )}
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1 },
  center: { alignItems: 'center', justifyContent: 'center', gap: 10, padding: 28 },
  centerBody: { maxWidth: 320, textAlign: 'center' },
  centerActions: { marginTop: 8 },
  cta: {
    minHeight: 48,
    borderRadius: 999,
    paddingHorizontal: 28,
    alignItems: 'center',
    justifyContent: 'center',
  },
  headerRow: {
    flexDirection: 'row',
    justifyContent: 'flex-end',
    paddingHorizontal: 20,
    paddingVertical: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  newChat: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    borderRadius: 999,
    paddingHorizontal: 16,
    paddingVertical: 8,
  },
  offlineBanner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    paddingHorizontal: 20,
    paddingVertical: 8,
  },
  errorBanner: {
    marginHorizontal: 20,
    marginTop: 10,
    borderRadius: 12,
    borderWidth: 1,
    padding: 12,
  },
  emptyIcon: {
    width: 84,
    height: 84,
    borderRadius: 28,
    alignItems: 'center',
    justifyContent: 'center',
  },
  list: { padding: 20, gap: 10 },
  listLabel: { marginLeft: 2, marginBottom: 2 },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    borderRadius: 18,
    borderWidth: 1,
    padding: 14,
  },
  rowIcon: {
    width: 40,
    height: 40,
    borderRadius: 13,
    alignItems: 'center',
    justifyContent: 'center',
  },
  rowTexts: { flex: 1, gap: 2 },
  delBtn: { paddingLeft: 6 },
});

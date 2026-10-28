import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  TextInput,
  View,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Clipboard from 'expo-clipboard';
import type { BottomTabScreenProps } from '@react-navigation/bottom-tabs';
import type { MainTabParamList } from '../navigation/types';
import { useTheme } from '../theme';
import { StewardText } from '../components/StewardText';
import { Card } from '../components/Card';
import { Button } from '../components/Button';
import { haptic } from '../lib/haptics';
import { rnStorage, secureTokenStore } from '../lib/storage';
import { ConversationCache } from '../lib/cache';
import { SessionManager } from '../lib/session';
import { StewardClient, StewardError } from '../lib/client';
import { sessionStore } from '../lib/sessionStore';
import { useSession } from '../hooks/useSession';
import { useUpdateNotice } from '../hooks/useUpdateNotice';
import { UpdateNotice } from '../components/UpdateNotice';
import { config } from '../config';
import type { ChatMessage, Session } from '../lib/types';

type Props = BottomTabScreenProps<MainTabParamList, 'Chat'>;

const MAX_ASSISTANT_PREVIEW = 200000;

export function ChatScreen({ navigation, route }: Props) {
  const t = useTheme();
  const c = t.colors;
  const session = useSession();

  const cache = useMemo(() => new ConversationCache(rnStorage), []);
  const sessions = useMemo(() => new SessionManager(rnStorage, secureTokenStore), []);
  const client = useMemo(
    () => (session ? StewardClient.fromSession(session, cache) : null),
    [session, cache],
  );

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [title, setTitle] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);
  const [online, setOnline] = useState<boolean | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [installingUpdate, setInstallingUpdate] = useState(false);
  const [updateResult, setUpdateResult] = useState<{
    ok: boolean;
    detail: string;
    link: string | null;
  } | null>(null);

  const updateNotice = useUpdateNotice({
    storage: rnStorage,
    clientFactory: (s) => StewardClient.fromSession(s, cache),
  });

  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const controllerRef = useRef<AbortController | null>(null);
  const paramConversationId = route.params?.conversationId;

  // Reachability dot (open route, no token needed).
  useEffect(() => {
    let alive = true;
    if (!session) {
      setOnline(false);
      return;
    }
    const clientHere = StewardClient.fromSession(session, cache);
    clientHere
      .health()
      .then(() => {
        if (alive) setOnline(true);
      })
      .catch(() => {
        if (alive) setOnline(false);
      });
    return () => {
      alive = false;
    };
  }, [session, cache]);

  const toChatMessages = useCallback((conv: {
    messages: { id: number | string; role: string; content: string }[];
  }): ChatMessage[] => {
    return conv.messages.map((m) => ({
      id: String(m.id),
      role: m.role === 'user' ? ('user' as const) : ('assistant' as const),
      text: m.content,
    }));
  }, []);

  const loadConversation = useCallback(
    async (id: number) => {
      if (!client) return;
      setLoading(true);
      setError(null);
      try {
        const { conversation, fromCache } = await client.openConversation(id);
        setMessages(toChatMessages(conversation));
        setConversationId(conversation.id);
        setTitle(conversation.title);
        setOffline(fromCache);
      } catch (err) {
        setConversationId(null);
        if (err instanceof StewardError && err.status === 401) {
          setError('Daemon rejected the session token. Re-pair in Settings.');
        } else {
          setError('Could not open this conversation while the daemon is unreachable and it is not cached.');
        }
      } finally {
        setLoading(false);
      }
    },
    [client, toChatMessages],
  );

  const recoverLast = useCallback(async () => {
    if (!client) return;
    setLoading(true);
    setOffline(false);
    try {
      const { summaries, fromCache } = await client.listConversations();
      if (summaries.length > 0) {
        await loadConversation(summaries[0].id);
        return;
      }
      // Fresh daemon, or daemon not paired yet. Fall through to cache.
      setMessages([]);
      setConversationId(null);
      setTitle(null);
      setOffline(fromCache);
    } catch {
      const cached = await client.latestCached();
      if (cached) {
        setMessages(toChatMessages(cached));
        setConversationId(cached.id);
        setTitle(cached.title);
        setOffline(true);
      } else {
        setMessages([]);
        setConversationId(null);
        setTitle(null);
        setOffline(false);
      }
    } finally {
      setLoading(false);
    }
  }, [client, loadConversation, toChatMessages]);

  // Load on session / explicit conversation open.
  useEffect(() => {
    if (!client) return;
    if (paramConversationId === null) {
      setMessages([]);
      setConversationId(null);
      setTitle(null);
      setError(null);
      return;
    }
    if (typeof paramConversationId === 'number') {
      loadConversation(paramConversationId);
      return;
    }
    if (conversationId === null) {
      recoverLast();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, paramConversationId]);

  const isAbort = (err: unknown): boolean =>
    err instanceof StewardError && err.kind === 'aborted';

  const streamReply = useCallback(
    async (text: string, pre: ChatMessage[]) => {
      if (!client) return;
      let convId = conversationId;
      let convTitle = title;
      if (convId === null) {
        try {
          const created = await client.createConversation(text.slice(0, 48));
          convId = created.id;
          convTitle = created.title;
          setConversationId(convId);
          setTitle(convTitle);
        } catch {
          // Daemon unreachable: stream without persistence, keep a local copy.
          convId = null;
        }
      }
      const now = Date.now();
      const userMsg: ChatMessage = { id: `msg-${now}-u`, role: 'user', text };
      const assistantId = `msg-${now}-a`;
      setMessages([...pre, userMsg, { id: assistantId, role: 'assistant', text: '' }]);
      const controller = new AbortController();
      controllerRef.current = controller;
      setStreaming(true);
      setStopped(false);
      setError(null);
      let full = '';
      try {
        await client.streamChat({
          history: pre.map((m) => ({ role: m.role, text: m.text })),
          text,
          conversationId: convId,
          signal: controller.signal,
          onDelta: (d) => {
            full += d;
            setMessages((prev) =>
              prev.map((m) => (m.id === assistantId ? { ...m, text: full } : m)),
            );
          },
        });
      } catch (err) {
        if (isAbort(err)) {
          setStopped(true);
        } else {
          const msg =
            err instanceof StewardError && err.status === 401
              ? 'Daemon rejected the session token. Re-pair in Settings.'
              : err instanceof Error
                ? err.message
                : String(err);
          setError(msg);
        }
      } finally {
        setStreaming(false);
        controllerRef.current = null;
        if (convId !== null && full.length <= MAX_ASSISTANT_PREVIEW) {
          const all = [
            ...pre,
            userMsg,
            { id: assistantId, role: 'assistant' as const, text: full },
          ];
          await client.cacheConversation(
            client.buildCachedConversation({
              id: convId,
              title: convTitle ?? 'New conversation',
              createdAt: new Date().toISOString(),
              messages: all.map((m) => ({ role: m.role, content: m.text })),
            }),
          );
        }
      }
    },
    [client, conversationId, title],
  );

  const submit = useCallback(() => {
    const text = input.trim();
    if (!text || streaming || !client) return;
    setInput('');
    const pre = messagesRef.current;
    void streamReply(text, pre);
  }, [input, streaming, client, streamReply]);

  const stop = useCallback(() => {
    controllerRef.current?.abort();
    haptic.light();
  }, []);

  const copy = useCallback(async (id: string, text: string) => {
    await Clipboard.setStringAsync(text);
    setCopiedId(id);
    haptic.light();
    setTimeout(() => setCopiedId((cur) => (cur === id ? null : cur)), 1400);
  }, []);

  const retry = useCallback(() => {
    if (!client || streaming) return;
    const list = messagesRef.current;
    const lastUser = [...list].reverse().find((m) => m.role === 'user');
    if (!lastUser) return;
    const idx = list.map((m) => m.id).lastIndexOf(lastUser.id);
    const pre = list.slice(0, idx + 1);
    setMessages(pre);
    setError(null);
    setStopped(false);
    void streamReply(lastUser.text, pre);
  }, [client, streaming, streamReply]);

  const unpair = useCallback(async () => {
    controllerRef.current?.abort();
    await sessions.clear();
    sessionStore.set(null);
    setMessages([]);
    setConversationId(null);
    setTitle(null);
    haptic.warning();
  }, [sessions]);

  const installUpdate = useCallback(async () => {
    const plan = updateNotice.state.plan;
    if (!client || !plan || installingUpdate) return;
    setInstallingUpdate(true);
    setUpdateResult(null);
    try {
      const report = await client.checkUpdate();
      const { Linking, Share } = await import('react-native');
      const outcome = await import('../lib/install').then((m) =>
        m.installMobileUpdate(report, config.version, {
          open: async (url) => {
            try {
              await Linking.openURL(url);
              return true;
            } catch {
              return false;
            }
          },
          share: async (url, message) => {
            try {
              await Share.share({ message });
              return true;
            } catch {
              return false;
            }
          },
        }),
      );
      setUpdateResult({
        ok: outcome.ok,
        detail: outcome.detail,
        link: outcome.link,
      });
    } catch (err) {
      setUpdateResult({
        ok: false,
        detail: err instanceof Error ? err.message : String(err),
        link: null,
      });
    } finally {
      setInstallingUpdate(false);
    }
  }, [client, updateNotice.state.plan, installingUpdate]);

  if (!session) {
    return (
      <View style={[styles.screen, styles.center, { backgroundColor: c.bg }]}>
        <View style={[styles.emptyIcon, { backgroundColor: c.brandContainer }]}>
          <Ionicons name="key-outline" size={40} color={c.brand} />
        </View>
        <StewardText variant="headline" align="center">
          Not paired yet
        </StewardText>
        <StewardText variant="body" secondary align="center" style={styles.emptyBody}>
          Pair this phone with a Steward daemon on your LAN to chat with the
          model that runs on your hardware.
        </StewardText>
        <View style={styles.emptyActions}>
          <Button
            label="Pair a daemon"
            icon="qr-code-outline"
            onPress={() => navigation.navigate('Pair')}
          />
        </View>
      </View>
    );
  }

  const data = [...messages].reverse();
  const lastAssistantId = !streaming
    ? [...messages].reverse().find((m) => m.role === 'assistant')?.id ?? null
    : null;

  return (
    <View style={[styles.screen, { backgroundColor: c.bg }]}>
      <View style={[styles.statusBar, { borderBottomColor: c.separator, backgroundColor: c.surface }]}>
        <View style={[styles.dot, { backgroundColor: online === null ? c.textTertiary : online ? c.success : c.warning }]} />
        <StewardText variant="caption" tertiary numberOfLines={1} style={styles.statusText}>
          {offline ? 'offline · cached conversation' : `daemon ${session.host}:${session.port}`}
          {online === false ? ' · unreachable' : online === true ? ' · online' : ''}
        </StewardText>
        <Pressable onPress={unpair} hitSlop={8} style={styles.statusAction}>
          <Ionicons name="link-outline" size={17} color={c.textTertiary} />
        </Pressable>
      </View>

      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={Platform.OS === 'ios' ? 96 : 0}
      >
        {title && !loading ? (
          <View style={styles.titleRow}>
            <StewardText variant="caption" tertiary numberOfLines={1}>
              {title}
            </StewardText>
          </View>
        ) : null}

        {loading ? (
          <View style={styles.center}>
            <ActivityIndicator size="large" color={c.brand} />
            <StewardText variant="caption" tertiary>Loading conversation</StewardText>
          </View>
        ) : (
          <FlatList
            data={data}
            inverted
            contentContainerStyle={styles.list}
            keyExtractor={(m) => m.id}
            ListHeaderComponent={
              <View style={styles.listHeader}>
                {updateNotice.state.plan?.available ? (
                  <UpdateNotice
                    plan={updateNotice.state.plan}
                    storage={rnStorage}
                    installing={installingUpdate}
                    onInstall={() => void installUpdate()}
                  />
                ) : null}
                {updateResult ? (
                  <View
                    style={[
                      styles.errorBanner,
                      { backgroundColor: updateResult.ok ? c.surface : c.warning, borderColor: c.border },
                    ]}
                  >
                    <Ionicons
                      name={updateResult.ok ? 'checkmark-circle-outline' : 'alert-circle-outline'}
                      size={16}
                      color={c.onBrand}
                    />
                    <StewardText variant="caption" style={styles.errorBannerText}>
                      {updateResult.detail}
                      {updateResult.link ? `\n${updateResult.link}` : ''}
                    </StewardText>
                  </View>
                ) : null}
                {error ? (
                  <View style={[styles.errorBanner, { backgroundColor: c.warning, borderColor: c.border }]}>
                    <Ionicons name="alert-circle-outline" size={16} color={c.onBrand} />
                    <StewardText variant="caption" style={styles.errorBannerText}>{error}</StewardText>
                  </View>
                ) : messages.length === 0 ? (
                  <StewardText variant="caption" tertiary align="center" style={styles.dayLabel}>
                    No conversation yet. Ask anything; it runs on your hardware.
                  </StewardText>
                ) : (
                  <StewardText variant="caption" tertiary align="center" style={styles.dayLabel}>
                    {streaming ? 'Steward is answering' : stopped ? 'Stopped' : 'Conversation stays on your network'}
                  </StewardText>
                )}
              </View>
            }
            renderItem={({ item }) => {
              const isUser = item.role === 'user';
              const showCopy = messages.length > 0;
              const showRetry =
                !isUser &&
                !streaming &&
                (stopped || Boolean(error)) &&
                item.id === lastAssistantId;
              return (
                <View style={[styles.bubbleWrap, isUser ? styles.rowUser : styles.rowAssistant]}>
                  <View
                    style={[
                      styles.bubble,
                      isUser
                        ? { backgroundColor: c.brand, borderTopRightRadius: 6 }
                        : {
                            backgroundColor: c.surface,
                            borderColor: c.border,
                            borderWidth: 1,
                            borderTopLeftRadius: 6,
                          },
                    ]}
                  >
                    <StewardText
                      variant="body"
                      color={isUser ? c.onBrand : c.textPrimary}
                      style={styles.bubbleText}
                    >
                      {item.text}
                    </StewardText>
                    {streaming && item.id === lastAssistantId ? (
                      <View style={styles.typingWrap}>
                        <ActivityIndicator size="small" color={c.onBrand} />
                      </View>
                    ) : null}
                  </View>
                  {showCopy ? (
                    <View style={styles.bubbleActions}>
                      <Pressable
                        onPress={() => void copy(item.id, item.text)}
                        hitSlop={8}
                        style={styles.actionBtn}
                        accessibilityRole="button"
                        accessibilityLabel="Copy message"
                      >
                        <Ionicons
                          name={copiedId === item.id ? 'checkmark' : 'copy-outline'}
                          size={14}
                          color={copiedId === item.id ? c.success : c.textTertiary}
                        />
                      </Pressable>
                      {showRetry ? (
                        <Pressable
                          onPress={retry}
                          hitSlop={8}
                          style={styles.actionBtn}
                          accessibilityRole="button"
                          accessibilityLabel="Retry"
                        >
                          <Ionicons name="refresh-outline" size={14} color={c.brand} />
                        </Pressable>
                      ) : null}
                    </View>
                  ) : null}
                </View>
              );
            }}
          />
        )}

        <Card padding={8} style={[styles.composer, { borderColor: c.border }]}>
          <TextInput
            value={input}
            onChangeText={setInput}
            placeholder="Ask your Steward anything"
            placeholderTextColor={c.textTertiary}
            style={[styles.input, { color: c.textPrimary, backgroundColor: c.bg }]}
            multiline
            returnKeyType="send"
            onSubmitEditing={submit}
            blurOnSubmit={false}
            editable={!streaming}
          />
          {streaming ? (
            <Pressable
              onPress={stop}
              style={[styles.send, { backgroundColor: c.warning }]}
              accessibilityRole="button"
              accessibilityLabel="Stop generation"
            >
              <Ionicons name="stop" size={18} color={c.onBrand} />
            </Pressable>
          ) : (
            <Pressable
              onPress={submit}
              style={[styles.send, { backgroundColor: input.trim() ? c.brand : c.separator }]}
              accessibilityRole="button"
              accessibilityLabel="Send"
            >
              <Ionicons
                name="arrow-up"
                size={20}
                color={input.trim() ? c.onBrand : c.textTertiary}
              />
            </Pressable>
          )}
        </Card>
      </KeyboardAvoidingView>
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  screen: { flex: 1 },
  center: { alignItems: 'center', justifyContent: 'center', gap: 10, padding: 24 },
  emptyIcon: {
    width: 84,
    height: 84,
    borderRadius: 28,
    alignItems: 'center',
    justifyContent: 'center',
  },
  emptyBody: { marginTop: 4, maxWidth: 320, textAlign: 'center' },
  emptyActions: { marginTop: 14, alignSelf: 'stretch', paddingHorizontal: 40, gap: 10 },
  statusBar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 7,
    paddingHorizontal: 20,
    paddingVertical: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  dot: { width: 8, height: 8, borderRadius: 4 },
  statusText: { flex: 1 },
  statusAction: { paddingLeft: 6 },
  titleRow: { paddingHorizontal: 20, paddingTop: 6 },
  list: { paddingHorizontal: 20, paddingVertical: 14, gap: 12 },
  listHeader: { gap: 8 },
  errorBanner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    borderRadius: 12,
    borderWidth: 1,
    paddingHorizontal: 12,
    paddingVertical: 8,
  },
  errorBannerText: { flex: 1 },
  dayLabel: { paddingVertical: 4 },
  bubbleWrap: { flexDirection: 'row', alignItems: 'flex-end', gap: 8 },
  rowUser: { justifyContent: 'flex-end' },
  rowAssistant: { justifyContent: 'flex-start' },
  bubble: {
    maxWidth: '82%',
    borderRadius: 18,
    paddingHorizontal: 16,
    paddingVertical: 11,
  },
  bubbleText: { fontSize: 16, lineHeight: 23 },
  bubbleActions: { flexDirection: 'row', gap: 10, paddingHorizontal: 4, paddingTop: 4 },
  actionBtn: { padding: 2 },
  typingWrap: { flexDirection: 'row', marginTop: 6 },
  composer: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: 8,
    margin: 12,
  },
  input: {
    flex: 1,
    minHeight: 44,
    maxHeight: 130,
    borderRadius: 14,
    paddingHorizontal: 14,
    paddingVertical: 10,
    fontSize: 16,
    lineHeight: 22,
  },
  send: {
    width: 44,
    height: 44,
    borderRadius: 22,
    alignItems: 'center',
    justifyContent: 'center',
  },
});

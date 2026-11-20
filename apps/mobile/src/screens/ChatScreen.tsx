import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  KeyboardAvoidingView,
  Modal,
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
import type { Palette } from '../theme';
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
import type { ChatMessage, Session, SourceRef, ToolStep } from '../lib/types';
import { approvalStatus, applyToolEvent, summarizeToolArgs } from '../lib/tools';

type Props = BottomTabScreenProps<MainTabParamList, 'Chat'>;

const MAX_ASSISTANT_PREVIEW = 200000;
const MAX_TOOL_SUMMARY = 96;

function sourceRange(s: SourceRef): string {
  if (typeof s.start_line === 'number' && typeof s.end_line === 'number') {
    return s.start_line === s.end_line
      ? `line ${s.start_line}`
      : `lines ${s.start_line}-${s.end_line}`;
  }
  return '';
}

/** The compact Sources disclosure under a grounded assistant reply (F4 wire).
 * Collapsed to a header by default; expanding reveals each source's path,
 * line range and snippet. */
function SourceBlock({ sources, colors }: { sources: SourceRef[]; colors: Palette }) {
  const [open, setOpen] = useState(false);
  if (!sources || sources.length === 0) return null;
  return (
    <Pressable
      onPress={() => {
        setOpen(!open);
        haptic.light();
      }}
      accessibilityRole="button"
      accessibilityLabel={`Show ${sources.length} sources`}
      style={({ pressed }) => [
        styles.sourcesCard,
        { backgroundColor: colors.surfaceElevated, borderColor: colors.border, opacity: pressed ? 0.85 : 1 },
      ]}
    >
      <View style={styles.sourcesHeader}>
        <StewardText variant="caption" secondary>
          Sources ({sources.length})
        </StewardText>
        <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={14} color={colors.textTertiary} />
      </View>
      {open ? (
        <View style={styles.sourcesList}>
          {sources.map((s) => (
            <View key={s.n} style={[styles.sourceRow, { borderTopColor: colors.border }]}>
              <StewardText variant="caption" tertiary numberOfLines={1}>
                [{s.n}] {s.path}
                {sourceRange(s) ? ` ${sourceRange(s)}` : ''}
              </StewardText>
              {s.snippet ? (
                <StewardText variant="caption" secondary numberOfLines={3} style={styles.sourceSnippet}>
                  {s.snippet}
                </StewardText>
              ) : null}
            </View>
          ))}
        </View>
      ) : null}
    </Pressable>
  );
}

/** One inline tool step rendered inside an assistant bubble (F8). */
function ToolLine({
  step,
  now,
  colors,
  onTap,
}: {
  step: ToolStep;
  now: number;
  colors: Palette;
  onTap: () => void;
}) {
  const summary =
    step.summary && step.summary.length > MAX_TOOL_SUMMARY
      ? `${step.summary.slice(0, MAX_TOOL_SUMMARY)}...`
      : step.summary ?? '';
  const suffix = step.ms != null ? ` in ${step.ms} ms` : '';
  const rowStyle = [styles.toolLine, { backgroundColor: colors.bg, borderColor: colors.border }];

  if (step.state === 'running') {
    return (
      <View style={rowStyle}>
        <ActivityIndicator size="small" color={colors.brand} />
        <StewardText variant="caption" secondary numberOfLines={1} style={styles.toolLineText}>
          {step.tool} running
        </StewardText>
      </View>
    );
  }

  if (step.state === 'approval') {
    const status = approvalStatus(step.expiresAt, now);
    return (
      <Pressable
        onPress={onTap}
        accessibilityRole="button"
        accessibilityLabel={`${step.tool} needs approval`}
        style={({ pressed }) => [
          rowStyle,
          { borderColor: colors.warning, opacity: pressed ? 0.8 : 1 },
        ]}
      >
        <View style={[styles.toolStatusDot, { backgroundColor: colors.warning }]} />
        <StewardText variant="caption" secondary numberOfLines={1} style={styles.toolLineText}>
          {step.tool} needs approval
        </StewardText>
        <StewardText variant="caption" tertiary numberOfLines={1} style={styles.toolLineMeta}>
          {status.expired ? 'expired, ask again' : status.label}
        </StewardText>
        <Ionicons name="chevron-forward" size={14} color={colors.textTertiary} />
      </Pressable>
    );
  }

  if (step.state === 'expired') {
    return (
      <View style={[rowStyle, { opacity: 0.6 }]}>
        <View style={[styles.toolStatusDot, { backgroundColor: colors.textTertiary }]} />
        <StewardText variant="caption" tertiary numberOfLines={1} style={styles.toolLineText}>
          {step.tool} expired, ask again
        </StewardText>
      </View>
    );
  }

  const done = step.state === 'done';
  const iconColor = done ? colors.success : colors.danger;
  return (
    <View style={rowStyle}>
      <Ionicons name={done ? 'checkmark-circle' : 'close-circle'} size={16} color={iconColor} />
      <StewardText
        variant="caption"
        secondary
        numberOfLines={1}
        style={styles.toolLineText}
      >
        {summary}
        {suffix}
      </StewardText>
    </View>
  );
}

/** Bottom-sheet approval prompt with Approve / Deny and an expiry countdown. */
function ApprovalSheet({
  approval,
  now,
  colors,
  onApprove,
  onDeny,
  onDismiss,
}: {
  approval: {
    id: string;
    tool: string;
    args: Record<string, unknown>;
    risk: string;
    expiresAt: string;
    deciding: boolean;
    error: string | null;
  };
  now: number;
  colors: Palette;
  onApprove: () => void;
  onDeny: () => void;
  onDismiss: () => void;
}) {
  const status = approvalStatus(approval.expiresAt, now);
  const risk = approval.risk || 'write';
  const riskColor = risk === 'read' ? colors.brand : risk === 'write' ? colors.warning : colors.danger;
  const locked = status.expired || approval.deciding;
  return (
    <Modal visible transparent animationType="fade" onRequestClose={onDismiss}>
      <Pressable
        style={[styles.sheetBackdrop, { backgroundColor: colors.scrim }]}
        onPress={onDismiss}
        accessibilityLabel="Dismiss approval"
      />
      <View style={[styles.sheet, { backgroundColor: colors.surfaceElevated }]}>
        <View style={styles.sheetHeader}>
          <View style={[styles.riskPill, { borderColor: riskColor }]}>
            <StewardText variant="caption" style={{ color: riskColor, fontWeight: '700' }}>
              {risk.toUpperCase()}
            </StewardText>
          </View>
          <StewardText variant="headline" numberOfLines={1} style={styles.sheetTitle}>
            {approval.tool}
          </StewardText>
        </View>
        <StewardText variant="body" secondary style={styles.sheetArgs}>
          {summarizeToolArgs(approval.tool, approval.args)}
        </StewardText>
        <StewardText variant="caption" tertiary style={styles.sheetStatus}>
          {status.expired ? 'expired, ask again' : status.label}
        </StewardText>
        {approval.error ? (
          <StewardText variant="caption" style={{ color: colors.danger, marginTop: 6 }}>
            {approval.error}
          </StewardText>
        ) : null}
        <View style={styles.sheetActions}>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Approve tool call"
            disabled={locked}
            onPress={onApprove}
            style={({ pressed }) => [
              styles.sheetButton,
              { backgroundColor: colors.success, opacity: locked ? 0.5 : pressed ? 0.85 : 1 },
            ]}
          >
            <Ionicons name="checkmark-circle-outline" size={20} color={colors.onBrand} />
            <StewardText variant="label" color={colors.onBrand}>
              {approval.deciding ? 'Approving...' : 'Approve'}
            </StewardText>
          </Pressable>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Deny tool call"
            disabled={locked}
            onPress={onDeny}
            style={({ pressed }) => [
              styles.sheetButton,
              {
                backgroundColor: colors.surfaceElevated,
                borderWidth: 1,
                borderColor: colors.danger,
                opacity: locked ? 0.5 : pressed ? 0.85 : 1,
              },
            ]}
          >
            <Ionicons name="close-circle-outline" size={20} color={colors.danger} />
            <StewardText variant="label" style={{ color: colors.danger }}>
              {approval.deciding ? 'Denying...' : 'Deny'}
            </StewardText>
          </Pressable>
        </View>
      </View>
    </Modal>
  );
}

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

  // Active approval shown in the bottom sheet. The stream id is the id the
  // daemon accepts on POST /v1/approvals/{id}.
  const [pendingApproval, setPendingApproval] = useState<{
    id: string;
    tool: string;
    args: Record<string, unknown>;
    risk: string;
    expiresAt: string;
    deciding: boolean;
    error: string | null;
  } | null>(null);
  const [now, setNow] = useState(() => Date.now());

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
    messages: { id: number | string; role: string; content: string; sources?: SourceRef[] }[];
  }): ChatMessage[] => {
    return conv.messages.map((m) => ({
      id: String(m.id),
      role: m.role === 'user' ? ('user' as const) : ('assistant' as const),
      text: m.content,
      sources: m.sources,
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
      setMessages([...pre, userMsg, { id: assistantId, role: 'assistant', text: '', tools: [] }]);
      const controller = new AbortController();
      controllerRef.current = controller;
      setStreaming(true);
      setStopped(false);
      setError(null);
      let full = '';
      let turnSources: SourceRef[] = [];
      const patchAssistantTools = (apply: (steps: ToolStep[]) => ToolStep[]) => {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, tools: apply(m.tools ?? []) } : m)),
        );
      };
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
          onSources: (srcs) => {
            // The final sources payload of this grounded turn (F4 wire):
            // rendered under the reply and stored on the message so the
            // offline cache keeps it.
            turnSources = srcs;
            setMessages((prev) =>
              prev.map((m) => (m.id === assistantId ? { ...m, sources: srcs } : m)),
            );
          },
          onEvent: (evt) => {
            // Tool frames update the inline lines inside this turn; they are
            // never part of the streamed text, so the offline cache stores
            // only the final reply.
            patchAssistantTools((steps) => applyToolEvent(steps, evt));
            if (evt.type === 'approval_required') {
              setPendingApproval({
                id: evt.id,
                tool: evt.tool,
                args: evt.args,
                risk: evt.risk,
                expiresAt: evt.expires_at,
                deciding: false,
                error: null,
              });
              setNow(Date.now());
            } else if (evt.type === 'tool_result') {
              setPendingApproval((cur) => (cur && cur.id === evt.id ? null : cur));
            }
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
            { id: assistantId, role: 'assistant' as const, text: full, sources: turnSources },
          ];
          await client.cacheConversation(
            client.buildCachedConversation({
              id: convId,
              title: convTitle ?? 'New conversation',
              createdAt: new Date().toISOString(),
              messages: all.map((m) => ({
                role: m.role,
                content: m.text,
                sources: m.sources,
              })),
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

  // Tick the approval countdown once a second while the sheet is open.
  useEffect(() => {
    if (!pendingApproval?.expiresAt) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingApproval]);

  const decideApproval = useCallback(
    async (decision: 'approve' | 'deny') => {
      if (!client || !pendingApproval || pendingApproval.deciding) return;
      const target = pendingApproval;
      setPendingApproval((cur) => (cur ? { ...cur, deciding: true, error: null } : cur));
      try {
        await client.decideApproval(target.id, decision);
        haptic.light();
        // The daemon resumes the turn and streams tool_result, which settles
        // the inline line; close the sheet either way.
        setPendingApproval((cur) => (cur?.id === target.id ? null : cur));
        setNow(Date.now());
      } catch (err) {
        const msg =
          err instanceof StewardError && err.status === 401
            ? 'Daemon rejected the session token. Re-pair in Settings.'
            : err instanceof Error
              ? err.message
              : String(err);
        setPendingApproval((cur) => (cur?.id === target.id ? { ...cur, deciding: false, error: msg } : cur));
      }
    },
    [client, pendingApproval],
  );

  const reopenApproval = useCallback((step: ToolStep) => {
    if (!step.expiresAt) return;
    setPendingApproval({
      id: step.id,
      tool: step.tool,
      args: step.args,
      risk: step.risk,
      expiresAt: step.expiresAt,
      deciding: false,
      error: null,
    });
    setNow(Date.now());
  }, []);

  const dismissApproval = useCallback(() => {
    setPendingApproval((cur) => (cur && !cur.deciding ? null : cur));
  }, []);

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
                    {!isUser && item.tools && item.tools.length > 0 ? (
                      <View style={styles.turnTools}>
                        {item.tools.map((step) => (
                          <ToolLine
                            key={step.id}
                            step={step}
                            now={now}
                            colors={c}
                            onTap={() => reopenApproval(step)}
                          />
                        ))}
                      </View>
                    ) : null}
                    {!isUser && item.sources && item.sources.length > 0 ? (
                      <SourceBlock sources={item.sources} colors={c} />
                    ) : null}
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

      {pendingApproval ? (
        <ApprovalSheet
          approval={pendingApproval}
          now={now}
          colors={c}
          onApprove={() => void decideApproval('approve')}
          onDeny={() => void decideApproval('deny')}
          onDismiss={dismissApproval}
        />
      ) : null}
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
  turnTools: { marginTop: 8, gap: 6 },
  sourcesCard: {
    marginTop: 8,
    borderRadius: 10,
    borderWidth: 1,
    paddingHorizontal: 10,
    paddingVertical: 7,
    gap: 4,
  },
  sourcesHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  sourcesList: { gap: 6, marginTop: 4 },
  sourceRow: {
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingTop: 6,
    gap: 2,
  },
  sourceSnippet: { marginTop: 2 },
  toolLine: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    borderRadius: 10,
    borderWidth: 1,
    paddingHorizontal: 10,
    paddingVertical: 6,
  },
  toolLineText: { flex: 1 },
  toolLineMeta: { flexShrink: 1 },
  toolStatusDot: { width: 8, height: 8, borderRadius: 4 },
  sheetBackdrop: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 },
  sheet: {
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 0,
    borderTopLeftRadius: 24,
    borderTopRightRadius: 24,
    padding: 20,
    paddingBottom: 34,
    gap: 8,
  },
  sheetHeader: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  riskPill: {
    borderWidth: 1,
    borderRadius: 999,
    paddingHorizontal: 10,
    paddingVertical: 2,
  },
  sheetTitle: { flex: 1 },
  sheetArgs: { marginTop: 2 },
  sheetStatus: { marginTop: 2 },
  sheetActions: { flexDirection: 'row', gap: 10, marginTop: 14 },
  sheetButton: {
    flex: 1,
    minHeight: 52,
    borderRadius: 999,
    alignItems: 'center',
    justifyContent: 'center',
    flexDirection: 'row',
    gap: 8,
  },
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

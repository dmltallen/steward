import { useEffect, useState } from 'react';
import {
  FlatList,
  KeyboardAvoidingView,
  Platform,
  StyleSheet,
  TextInput,
  View,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import type { BottomTabScreenProps } from '@react-navigation/bottom-tabs';
import type { MainTabParamList } from '../navigation/types';
import { useTheme } from '../theme';
import { StewardText } from '../components/StewardText';
import { Card } from '../components/Card';
import { haptic } from '../lib/haptics';
import { config } from '../config';
import { ChatMessage, pingDaemon, sendChat } from '../lib/daemon';

type Props = BottomTabScreenProps<MainTabParamList, 'Chat'>;

const WELCOME: ChatMessage = {
  id: 'welcome',
  role: 'assistant',
  text: 'Hello. I am your Steward, running locally on hardware you own. Ask me anything; nothing here leaves your network.',
};

export function ChatScreen(_props: Props) {
  const t = useTheme();
  const c = t.colors;
  const [messages, setMessages] = useState<ChatMessage[]>([WELCOME]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [online, setOnline] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    pingDaemon().then((ok) => {
      if (alive) setOnline(ok);
    });
    return () => {
      alive = false;
    };
  }, []);

  const submit = async () => {
    const text = input.trim();
    if (!text || sending) return;
    const history = [...messages, { id: `user-${Date.now()}`, role: 'user' as const, text }];
    setMessages(history);
    setInput('');
    setSending(true);
    try {
      const reply = await sendChat(messages, text);
      haptic.success();
      setMessages((prev) => [...prev, reply]);
    } finally {
      setSending(false);
    }
  };

  const data = [...messages].reverse();

  return (
    <View style={[styles.screen, { backgroundColor: c.bg }]}>
      <View
        style={[
          styles.statusBar,
          { borderBottomColor: c.separator, backgroundColor: c.surface },
        ]}
      >
        <View
          style={[
            styles.dot,
            { backgroundColor: online === null ? c.textTertiary : online ? c.success : c.warning },
          ]}
        />
        <StewardText variant="caption" tertiary numberOfLines={1}>
          daemon {config.daemonUrl} · {online === null ? 'checking' : online ? 'online' : 'offline'}
        </StewardText>
      </View>

      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={Platform.OS === 'ios' ? 96 : 0}
      >
        <FlatList
          data={data}
          inverted
          contentContainerStyle={styles.list}
          keyExtractor={(m) => m.id}
          renderItem={({ item }) => {
            const isUser = item.role === 'user';
            return (
              <View
                style={[
                  styles.bubbleRow,
                  isUser ? styles.rowUser : styles.rowAssistant,
                ]}
              >
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
                </View>
              </View>
            );
          }}
          ListHeaderComponent={
            <StewardText variant="caption" tertiary align="center" style={styles.dayLabel}>
              {sending ? 'Steward is typing…' : 'Conversation stays on your network'}
            </StewardText>
          }
        />

        <Card padding={8} style={[styles.composer, { borderColor: c.border }]}>
          <TextInput
            value={input}
            onChangeText={setInput}
            placeholder="Ask your Steward anything"
            placeholderTextColor={c.textTertiary}
            style={[
              styles.input,
              { color: c.textPrimary, backgroundColor: c.bg },
            ]}
            multiline
            returnKeyType="send"
            onSubmitEditing={submit}
            blurOnSubmit={false}
          />
          <View
            style={[
              styles.send,
              { backgroundColor: input.trim() ? c.brand : c.separator },
            ]}
          >
            <Ionicons
              name="arrow-up"
              size={20}
              color={input.trim() ? c.onBrand : c.textTertiary}
              onPress={submit}
            />
          </View>
        </Card>
      </KeyboardAvoidingView>
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  screen: { flex: 1 },
  statusBar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 7,
    paddingHorizontal: 20,
    paddingVertical: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  dot: { width: 8, height: 8, borderRadius: 4 },
  list: { paddingHorizontal: 20, paddingVertical: 14, gap: 10 },
  bubbleRow: { flexDirection: 'row' },
  rowUser: { justifyContent: 'flex-end' },
  rowAssistant: { justifyContent: 'flex-start' },
  bubble: {
    maxWidth: '82%',
    borderRadius: 18,
    paddingHorizontal: 16,
    paddingVertical: 11,
  },
  bubbleText: { fontSize: 16, lineHeight: 23 },
  dayLabel: { paddingVertical: 6 },
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

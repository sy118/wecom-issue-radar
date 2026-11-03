import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type UIEvent,
} from "react";
import {
  AlertCircle,
  AlertTriangle,
  ArrowDown,
  Check,
  ChevronDown,
  Clipboard,
  Image as ImageIcon,
  KeyRound,
  LoaderCircle,
  MessageCircle,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Sparkles,
  User,
  Users,
} from "lucide-react";
import clsx from "clsx";
import { toast } from "sonner";

import { Button, Field, Input } from "../../components/ui";
import { bridge } from "../../lib/bridge";
import { copyToClipboard } from "../../lib/clipboard";
import { toUserErrorMessage } from "../../lib/errors";
import { createQuery } from "../../lib/replyRuntimeUi";
import {
  CONTEXT_PAGE_SIZE,
  CONTEXT_WINDOW_SECONDS,
  POLL_INTERVAL_MS,
  RECENT_CHAT_LIMIT,
  chatDisplayName,
  chatTypeLabel,
  chatsFingerprint,
  collectContextImageIds,
  composeContextText,
  contextFingerprint,
  defaultChatId,
  eqReplyPayload,
  eqStyleLabel,
  formatChatTime,
  formatMessageClock,
  isNearBottom,
  mergeContextMessages,
  messageBodyText,
  messageDisplayText,
  nextReplyTargetId,
  normalizeContext,
  normalizeEqResult,
  normalizeRecentChats,
  readCachedContext,
  replyDrafts,
  senderDisplayName,
  shouldCollapseMessage,
  shouldLoadOlder,
  shouldPoll,
  writeCachedContext,
  type ChatContextCache,
  type EqReplyResult,
  type WecomChatContext,
  type WecomContextMessage,
  type WecomRecentChat,
} from "./highEqReply.logic";
import "./highEqReply.css";

export interface HighEqReplyPageProps {
  /** App keeps the page mounted while hidden; false pauses polling. */
  active?: boolean;
  /** Navigation hook provided by App (settings guidance links). */
  onNavigate?: (page: string) => void;
}

/** Scroll bookkeeping after a context state update. */
type PendingScroll =
  | "bottom"
  | "smooth-bottom"
  | { previousHeight: number; previousTop: number };

/**
 * 企业微信 → 高情商回复.
 *
 * Reads the newest conversations and the open conversation's context through
 * the reply runtime (read-only). The context pane is a fixed-height scroll
 * viewport that starts at the newest message, pages older messages on scroll,
 * and keeps a per-conversation renderer cache so switching back is instant.
 * Images travel with the generation request; loading never OCRs them.
 * The page only copies to the clipboard: it never sends a message, never fills
 * the WeCom input box and never simulates a keystroke.
 */
export function HighEqReplyPage({ active = true, onNavigate }: HighEqReplyPageProps) {
  const [chats, setChats] = useState<WecomRecentChat[]>([]);
  const [chatsLoading, setChatsLoading] = useState(true);
  const [chatsError, setChatsError] = useState("");
  const [selectedChatId, setSelectedChatId] = useState("");
  const [context, setContext] = useState<WecomChatContext | null>(null);
  const [contextLoading, setContextLoading] = useState(false);
  const [contextError, setContextError] = useState("");
  const [contextUpdatedAt, setContextUpdatedAt] = useState(0);
  const [olderLoading, setOlderLoading] = useState(false);
  const [jumpToLatestVisible, setJumpToLatestVisible] = useState(false);
  const [targetId, setTargetId] = useState("");
  const [manualMode, setManualMode] = useState(false);
  const [manualText, setManualText] = useState("");
  const [recipient, setRecipient] = useState("");
  const [result, setResult] = useState<EqReplyResult | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState("");
  const [copiedStyle, setCopiedStyle] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const selectedChatIdRef = useRef("");
  const targetIdRef = useRef("");
  const contextRef = useRef<WecomChatContext | null>(null);
  const contextSeqRef = useRef(0);
  const contextFingerprintRef = useRef("");
  const contextCacheRef = useRef<ChatContextCache>({});
  const pagedConversationsRef = useRef<Set<string>>(new Set());
  const previousChatRef = useRef<string | null>(null);
  const pollingRef = useRef(false);
  const olderLoadingRef = useRef(false);
  const wasActiveRef = useRef(active);
  const copiedTimerRef = useRef<number | undefined>(undefined);
  const resultRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickToBottomRef = useRef(true);
  const initialScrollReadyRef = useRef(false);
  const pendingScrollRef = useRef<PendingScroll | null>(null);

  const selectChatId = useCallback((conversationId: string) => {
    selectedChatIdRef.current = conversationId;
    setSelectedChatId(conversationId);
  }, []);

  const applyTarget = useCallback((messageId: string) => {
    targetIdRef.current = messageId;
    setTargetId(messageId);
  }, []);

  const applyContext = useCallback((next: WecomChatContext) => {
    contextRef.current = next;
    setContext(next);
  }, []);

  /** Drops everything that only made sense for the previous conversation. */
  const resetConversationState = useCallback(() => {
    applyTarget("");
    setManualMode(false);
    setManualText("");
    setRecipient("");
    setResult(null);
    setDrafts({});
    setGenerateError("");
    setContextError("");
    setContext(null);
    contextRef.current = null;
    setContextUpdatedAt(0);
    setOlderLoading(false);
    olderLoadingRef.current = false;
    setJumpToLatestVisible(false);
    stickToBottomRef.current = true;
    initialScrollReadyRef.current = false;
    pendingScrollRef.current = null;
    contextFingerprintRef.current = "";
  }, [applyTarget]);

  const applyDefaultTarget = useCallback((messages: WecomContextMessage[], currentTargetId: string) => {
    const nextTarget = nextReplyTargetId(messages, currentTargetId);
    applyTarget(nextTarget);
    if (nextTarget !== currentTargetId) {
      const targetMessage = messages.find((message) => message.messageId === nextTarget);
      setRecipient(targetMessage
        ? senderDisplayName(targetMessage.senderName, targetMessage.senderId)
        : "");
    }
  }, [applyTarget]);

  const queryContextPage = useCallback(async (
    conversationId: string,
    options: { before?: number; refresh?: boolean } = {},
  ): Promise<WecomChatContext> => {
    const before = options.before && options.before > 0 ? Math.floor(options.before) : 0;
    const raw = await bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({
      kind: "chat.context",
      conversationId,
      limit: CONTEXT_PAGE_SIZE,
      windowSeconds: CONTEXT_WINDOW_SECONDS,
      ...(before > 0 ? { before } : {}),
      ...(options.refresh ? { refresh: true } : {}),
    }));
    return normalizeContext(raw);
  }, []);

  /**
   * Loads the newest page (or renders a cached copy first, then silently
   * refreshes it). Merges with already loaded older pages and never lets a
   * stale request overwrite a conversation the user already left.
   */
  const loadContext = useCallback(async (
    conversationId: string,
    options: { quiet?: boolean; refresh?: boolean; cached?: WecomChatContext | null } = {},
  ) => {
    if (!conversationId) return;
    const quiet = Boolean(options.quiet);
    const refresh = Boolean(options.refresh);
    const cached = options.cached ?? null;
    const seq = ++contextSeqRef.current;

    if (cached) {
      applyContext(cached);
      contextFingerprintRef.current = contextFingerprint(cached.messages);
      applyDefaultTarget(cached.messages, "");
      setContextLoading(false);
      // Cached conversations still open at the newest message.
      stickToBottomRef.current = true;
      initialScrollReadyRef.current = false;
      pendingScrollRef.current = "bottom";
    } else if (!quiet && seq === contextSeqRef.current) {
      setContextLoading(true);
    }

    try {
      const page = await queryContextPage(conversationId, { refresh });
      if (seq !== contextSeqRef.current || selectedChatIdRef.current !== conversationId) return;

      const previous = contextRef.current && contextRef.current.conversationId === conversationId
        ? contextRef.current
        : null;
      const hasLoadedOlder = pagedConversationsRef.current.has(conversationId);
      const mergedMessages = previous
        ? mergeContextMessages(previous.messages, page.messages)
        : page.messages;
      const previousLastId = previous?.messages[previous.messages.length - 1]?.messageId ?? "";
      const nextLastId = mergedMessages[mergedMessages.length - 1]?.messageId ?? "";
      const next: WecomChatContext = {
        ...page,
        messages: mergedMessages,
        // Once older pages are loaded, a latest-page poll must not claim the
        // history is complete again.
        hasMore: previous && hasLoadedOlder ? previous.hasMore : page.hasMore,
        windowFallback: page.windowFallback || Boolean(previous?.windowFallback),
      };

      setContextError("");
      setContextUpdatedAt(Date.now());
      const fingerprint = contextFingerprint(mergedMessages);
      const metaChanged = previous
        ? previous.hasMore !== next.hasMore || previous.windowFallback !== next.windowFallback
        : false;
      const changed = !previous || fingerprint !== contextFingerprintRef.current || metaChanged;
      if (changed) {
        contextFingerprintRef.current = fingerprint;
        applyContext(next);
        contextCacheRef.current = writeCachedContext(
          contextCacheRef.current,
          conversationId,
          next,
          Date.now(),
        );
        applyDefaultTarget(mergedMessages, targetIdRef.current);
        if (!previous) {
          // First page for this conversation: default to the newest message.
          stickToBottomRef.current = true;
          pendingScrollRef.current = "bottom";
        } else if (nextLastId !== previousLastId) {
          if (stickToBottomRef.current) {
            pendingScrollRef.current = "smooth-bottom";
          } else {
            setJumpToLatestVisible(true);
          }
        }
      } else {
        contextCacheRef.current = writeCachedContext(
          contextCacheRef.current,
          conversationId,
          next,
          Date.now(),
        );
      }
    } catch (error) {
      if (seq !== contextSeqRef.current || selectedChatIdRef.current !== conversationId) return;
      setContextError(toUserErrorMessage(error, "读取会话上下文失败，请稍后重试。"));
    } finally {
      if (!quiet && seq === contextSeqRef.current) setContextLoading(false);
    }
  }, [applyContext, applyDefaultTarget, queryContextPage]);

  /** Requests the page before the earliest loaded message and preserves scroll. */
  const loadOlderContext = useCallback(async () => {
    const conversationId = selectedChatIdRef.current;
    const current = contextRef.current;
    if (!conversationId || !current || current.conversationId !== conversationId) return;
    if (olderLoadingRef.current || !current.hasMore) return;
    const earliest = current.messages[0]?.sendTime ?? 0;
    if (!earliest) return;

    olderLoadingRef.current = true;
    setOlderLoading(true);
    const element = scrollRef.current;
    const previousHeight = element?.scrollHeight ?? 0;
    const previousTop = element?.scrollTop ?? 0;
    const seq = ++contextSeqRef.current;
    try {
      const page = await queryContextPage(conversationId, { before: earliest });
      if (seq !== contextSeqRef.current || selectedChatIdRef.current !== conversationId) return;
      const latest = contextRef.current;
      if (!latest || latest.conversationId !== conversationId) return;
      const mergedMessages = mergeContextMessages(latest.messages, page.messages);
      const next: WecomChatContext = {
        ...latest,
        messages: mergedMessages,
        hasMore: page.hasMore,
        windowFallback: latest.windowFallback || page.windowFallback,
      };
      pagedConversationsRef.current.add(conversationId);
      contextFingerprintRef.current = contextFingerprint(mergedMessages);
      applyContext(next);
      contextCacheRef.current = writeCachedContext(
        contextCacheRef.current,
        conversationId,
        next,
        Date.now(),
      );
      if (mergedMessages.length > latest.messages.length) {
        pendingScrollRef.current = { previousHeight, previousTop };
      }
      setContextError("");
    } catch (error) {
      if (seq === contextSeqRef.current && selectedChatIdRef.current === conversationId) {
        setContextError(toUserErrorMessage(error, "加载更早消息失败，请稍后重试。"));
      }
    } finally {
      // When a prepend is scheduled, the layout effect that restores the scroll
      // anchor also clears the loading flags. Clearing them here would let the
      // queued scroll event from "jump to top" start a second page request
      // before the first page is committed.
      const pending = pendingScrollRef.current;
      const preservesAnchor = pending !== null && typeof pending === "object";
      if (!preservesAnchor) {
        olderLoadingRef.current = false;
        setOlderLoading(false);
      }
    }
  }, [applyContext, queryContextPage]);

  const loadChats = useCallback(async (options: { quiet?: boolean; refresh?: boolean } = {}) => {
    const quiet = Boolean(options.quiet);
    const refresh = Boolean(options.refresh);
    if (!quiet) setChatsLoading(true);
    try {
      const raw = await bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({
        kind: "chat.recent",
        limit: RECENT_CHAT_LIMIT,
        ...(refresh ? { refresh: true } : {}),
      }));
      const next = normalizeRecentChats(raw);
      setChats((current) => (
        chatsFingerprint(current) === chatsFingerprint(next) ? current : next
      ));
      setChatsError("");
      const currentId = selectedChatIdRef.current;
      if (!next.some((chat) => chat.conversationId === currentId)) {
        selectChatId(defaultChatId(next));
      }
      return next;
    } catch (error) {
      setChatsError(toUserErrorMessage(error, "无法读取企业微信会话，请稍后重试。"));
      return [];
    } finally {
      if (!quiet) setChatsLoading(false);
    }
  }, [selectChatId]);

  const refreshAll = useCallback(async () => {
    setRefreshing(true);
    try {
      const nextChats = await loadChats({ quiet: true, refresh: true });
      const currentId = selectedChatIdRef.current;
      const conversationId = nextChats.some((chat) => chat.conversationId === currentId)
        ? currentId
        : defaultChatId(nextChats);
      if (conversationId) await loadContext(conversationId, { quiet: true, refresh: true });
    } finally {
      setRefreshing(false);
    }
  }, [loadChats, loadContext]);

  const refreshContext = useCallback(() => {
    const conversationId = selectedChatIdRef.current;
    if (conversationId) void loadContext(conversationId, { refresh: true });
  }, [loadContext]);

  /** Clicking a chat cancels any in-flight poll before the switch effect runs. */
  const selectChat = useCallback((conversationId: string) => {
    if (conversationId === selectedChatIdRef.current) return;
    contextSeqRef.current += 1;
    selectChatId(conversationId);
  }, [selectChatId]);

  // Initial chat list load bypasses the runtime directory cache.
  useEffect(() => {
    void loadChats({ refresh: true });
  }, [loadChats]);

  // Conversation switch: reset target/result, render cached context instantly
  // (if any), then refresh it in the background.
  useEffect(() => {
    const previous = previousChatRef.current;
    previousChatRef.current = selectedChatId;
    if (!selectedChatId) {
      if (previous !== null) resetConversationState();
      return;
    }
    if (previous !== selectedChatId) resetConversationState();
    const cached = readCachedContext(contextCacheRef.current, selectedChatId, Date.now());
    void loadContext(selectedChatId, cached ? { cached, quiet: true } : { refresh: true });
  }, [selectedChatId, loadContext, resetConversationState]);

  // Keep the newest page in view after a state update; restore the reading
  // anchor after an older page is prepended.
  useLayoutEffect(() => {
    const element = scrollRef.current;
    const pending = pendingScrollRef.current;
    if (pending === "bottom") {
      pendingScrollRef.current = null;
      initialScrollReadyRef.current = true;
      stickToBottomRef.current = true;
      setJumpToLatestVisible(false);
      if (element) element.scrollTop = element.scrollHeight;
      return;
    }
    if (pending === "smooth-bottom") {
      pendingScrollRef.current = null;
      initialScrollReadyRef.current = true;
      stickToBottomRef.current = true;
      setJumpToLatestVisible(false);
      if (element) {
        if (typeof element.scrollTo === "function") {
          element.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
        } else {
          element.scrollTop = element.scrollHeight;
        }
      }
      return;
    }
    if (pending) {
      pendingScrollRef.current = null;
      if (element) {
        element.scrollTop = element.scrollHeight - pending.previousHeight + pending.previousTop;
      }
      olderLoadingRef.current = false;
      setOlderLoading(false);
      return;
    }
    // Safety net: a conversation with a loaded context must land at the bottom
    // even if no explicit scroll request was recorded (e.g. remount).
    if (context && !initialScrollReadyRef.current) {
      initialScrollReadyRef.current = true;
      stickToBottomRef.current = true;
      if (element) element.scrollTop = element.scrollHeight;
    }
  }, [context]);

  const onContextScroll = (event: UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    const metrics = {
      scrollTop: element.scrollTop,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
    };
    stickToBottomRef.current = isNearBottom(metrics);
    if (stickToBottomRef.current) setJumpToLatestVisible(false);
    if (!initialScrollReadyRef.current) return;
    if (shouldLoadOlder({
      ...metrics,
      hasMore: contextRef.current?.hasMore ?? false,
      loadingOlder: olderLoadingRef.current,
    })) {
      void loadOlderContext();
    }
  };

  const jumpToLatest = () => {
    const element = scrollRef.current;
    stickToBottomRef.current = true;
    setJumpToLatestVisible(false);
    if (!element) return;
    if (typeof element.scrollTo === "function") {
      element.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
    } else {
      element.scrollTop = element.scrollHeight;
    }
  };

  // 30s polling while the page is active and the window is visible; a hidden
  // page never touches the WeCom database.
  useEffect(() => {
    if (!active) return;
    let disposed = false;
    const tick = async () => {
      if (disposed || pollingRef.current || olderLoadingRef.current) return;
      if (!shouldPoll(active, document.visibilityState)) return;
      pollingRef.current = true;
      try {
        await loadChats({ quiet: true });
        const conversationId = selectedChatIdRef.current;
        if (conversationId) await loadContext(conversationId, { quiet: true });
      } finally {
        pollingRef.current = false;
      }
    };
    const timer = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") void tick();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [active, loadChats, loadContext]);

  // Coming back from another route refreshes immediately instead of waiting
  // for the next interval tick.
  useEffect(() => {
    if (!active) {
      wasActiveRef.current = false;
      return;
    }
    if (!wasActiveRef.current) {
      wasActiveRef.current = true;
      void refreshAll();
    }
  }, [active, refreshAll]);

  useEffect(() => () => {
    if (copiedTimerRef.current) window.clearTimeout(copiedTimerRef.current);
  }, []);

  const selectedChat = useMemo(
    () => chats.find((chat) => chat.conversationId === selectedChatId) ?? null,
    [chats, selectedChatId],
  );
  const targetMessage = useMemo(
    () => context?.messages.find((message) => message.messageId === targetId) ?? null,
    [context, targetId],
  );
  const imageMessageIds = useMemo(
    () => collectContextImageIds(context?.messages ?? []),
    [context],
  );
  const contextText = useMemo(
    () => composeContextText(context?.messages ?? [], {}, {
      excludeMessageId: manualMode ? undefined : targetId,
    }),
    [context, manualMode, targetId],
  );
  const targetContent = manualMode
    ? manualText.trim()
    : targetMessage
      ? messageBodyText(targetMessage).trim()
      : "";
  const isGroupChat = context?.isGroup ?? selectedChat?.isGroup ?? false;
  const canGenerate = Boolean(targetContent) && !generating;
  const lastUpdatedLabel = contextUpdatedAt
    ? `更新于 ${new Date(contextUpdatedAt).toLocaleTimeString("zh-CN", { hour12: false })}`
    : "每 30 秒自动刷新";
  const contextMetaLabel = context
    ? [
        `已加载 ${context.messages.length} 条`,
        context.windowFallback ? "较早消息" : "最近 6 小时",
        context.hasMore ? "可加载更早" : "",
      ].filter(Boolean).join(" · ")
    : selectedChatId
      ? "正在读取最近消息…"
      : "选择左侧会话后自动读取";

  const chooseTarget = (message: WecomContextMessage) => {
    setManualMode(false);
    setGenerateError("");
    const changed = message.messageId !== targetIdRef.current;
    applyTarget(message.messageId);
    if (!changed) return;
    setRecipient(senderDisplayName(message.senderName, message.senderId));
    setResult(null);
    setDrafts({});
  };

  const updateDraft = (styleId: string, content: string) => {
    setDrafts((current) => ({ ...current, [styleId]: content }));
  };

  const copyStyle = async (styleId: string) => {
    const content = drafts[styleId] ?? "";
    if (!content.trim()) {
      toast.warning("这一版还是空的");
      return;
    }
    try {
      await copyToClipboard(content);
      setCopiedStyle(styleId);
      if (copiedTimerRef.current) window.clearTimeout(copiedTimerRef.current);
      copiedTimerRef.current = window.setTimeout(() => setCopiedStyle(""), 1_600);
      toast.success("已复制到剪贴板");
    } catch {
      toast.error("复制失败", {
        description: "剪贴板不可用，请手动选中文本复制；回复内容不会被修改。",
      });
    }
  };

  const extractKeys = async () => {
    try {
      await bridge.launchKeyExtraction();
      toast.success("已打开密钥提取窗口", {
        description: "按窗口提示提取，完成后回到本页点「刷新」。",
      });
    } catch (error) {
      toast.error("无法启动密钥提取", {
        description: toUserErrorMessage(error, "请稍后重试，或在「设置 → 企业微信」中手动处理。"),
      });
    }
  };

  const generate = async () => {
    if (generating) return;
    if (!targetContent) {
      setGenerateError(manualMode
        ? "请先粘贴对方的消息，再生成回复。"
        : "请先选择一条待回复消息，或切换到「手动输入」。");
      return;
    }
    const trimmedRecipient = recipient.trim()
      || (!manualMode && targetMessage
        ? senderDisplayName(targetMessage.senderName, targetMessage.senderId)
        : "");
    setGenerating(true);
    setGenerateError("");
    try {
      const raw = await bridge.assistantAction<unknown>(
        "eq_reply",
        eqReplyPayload({
          text: targetContent,
          context: contextText,
          recipient: trimmedRecipient,
          conversationId: context?.conversationId ?? "",
          imageMessageIds,
        }),
      );
      const normalized = normalizeEqResult(raw);
      if (normalized.styles.length === 0) {
        setGenerateError("这次没有生成可用的回复草稿，可以稍后重试，或把待回复内容缩短一些。");
        return;
      }
      setResult(normalized);
      setDrafts(replyDrafts(normalized));
      window.setTimeout(() => resultRef.current?.scrollIntoView({ block: "nearest" }), 60);
    } catch (error) {
      setGenerateError(toUserErrorMessage(error, "生成失败，请稍后重试。"));
    } finally {
      setGenerating(false);
    }
  };

  const onManualKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
    if (event.shiftKey || event.altKey || event.nativeEvent.isComposing) return;
    if (generating || !manualText.trim()) return;
    event.preventDefault();
    void generate();
  };

  return (
    <div className="page-content heq-page">
      <div className="page-title-row">
        <div>
          <div className="eyebrow"><Sparkles size={13} />企业微信 · 高情商回复</div>
          <h1>高情商回复</h1>
          <p>自动读取最近群聊/私聊，生成时结合聊天图片；先看潜台词，再挑一版回复。只复制，不会替你发送。</p>
        </div>
        <div className="runtime-header-actions heq-title-actions">
          <span className="heq-poll-note" role="status" aria-live="polite">
            <i className="heq-poll-dot" aria-hidden="true" />
            {lastUpdatedLabel}
          </span>
          <Button
            variant="secondary"
            onClick={() => void refreshAll()}
            disabled={refreshing}
            aria-busy={refreshing}
          >
            <RefreshCw size={14} className={refreshing ? "spin" : undefined} />
            {refreshing ? "刷新中" : "刷新"}
          </Button>
        </div>
      </div>

      {chatsError && chats.length > 0 && (
        <div className="heq-inline-error heq-page-error" role="alert">
          <AlertCircle size={15} />
          <span>{chatsError}</span>
          <button type="button" onClick={() => void refreshAll()}>重试</button>
        </div>
      )}

      <div className="heq-workspace">
        <aside className="heq-panel heq-rail" aria-label="最近会话">
          <header className="heq-panel-head">
            <div className="heq-panel-heading">
              <span className="eyebrow">RECENT CHATS</span>
              <h2>最近会话</h2>
              <small>群聊与私聊 · 按最后消息排序</small>
            </div>
            <div className="heq-panel-actions">
              <button
                type="button"
                className="heq-icon-button"
                title="刷新会话列表"
                aria-label="刷新会话列表"
                onClick={() => void loadChats({ refresh: true })}
                disabled={chatsLoading}
              >
                <RefreshCw size={14} className={chatsLoading ? "spin" : undefined} />
              </button>
            </div>
          </header>

          {chatsLoading && chats.length === 0 ? (
            <div className="heq-rail-state">
              <LoaderCircle className="spin" size={18} />
              <span>正在读取最近会话…</span>
            </div>
          ) : chatsError && chats.length === 0 ? (
            <div className="heq-rail-state" role="alert">
              <AlertCircle size={18} />
              <strong>无法读取会话</strong>
              <span>{chatsError}</span>
              <button type="button" className="heq-text-button" onClick={() => void loadChats({ refresh: true })}>
                重试
              </button>
            </div>
          ) : chats.length === 0 ? (
            <div className="heq-rail-state">
              <MessageCircle size={18} />
              <span>还没有可读取的会话</span>
            </div>
          ) : (
            <ul className="heq-rail-list">
              {chats.map((chat) => (
                <ChatListItem
                  key={chat.conversationId}
                  chat={chat}
                  active={chat.conversationId === selectedChatId}
                  onSelect={selectChat}
                />
              ))}
            </ul>
          )}
        </aside>

        <section className="heq-panel heq-context" aria-label="会话上下文">
          <header className="heq-panel-head">
            <div className="heq-panel-heading">
              <span className="eyebrow">CONTEXT</span>
              <h2>{selectedChat ? chatDisplayName(selectedChat) : context ? chatDisplayName(context) : "会话上下文"}</h2>
              <small>{contextMetaLabel}</small>
            </div>
            <div className="heq-panel-actions">
              {selectedChatId && (
                <span className={clsx("heq-badge", isGroupChat ? "is-group" : "is-direct")}>
                  {chatTypeLabel(isGroupChat)}
                </span>
              )}
              <button
                type="button"
                className="heq-icon-button"
                title="刷新当前会话"
                aria-label="刷新当前会话上下文"
                onClick={refreshContext}
                disabled={!selectedChatId || contextLoading}
              >
                <RefreshCw size={14} className={contextLoading ? "spin" : undefined} />
              </button>
            </div>
          </header>

          <div className="heq-context-body">
            {contextError && (
              <div className="heq-inline-error" role="alert">
                <AlertCircle size={15} />
                <span>{contextError}</span>
                <button type="button" onClick={refreshContext}>重试</button>
              </div>
            )}

            <div
              className="heq-context-stream"
              ref={scrollRef}
              onScroll={onContextScroll}
              aria-label="上下文消息"
            >
              {selectedChatId && context && context.messages.length > 0 && (
                <div className="heq-context-history" role="status" aria-live="polite">
                  {olderLoading ? (
                    <span><LoaderCircle className="spin" size={12} />加载更早…</span>
                  ) : context.hasMore ? (
                    <span>向上滚动加载更早消息</span>
                  ) : (
                    <span>没有更早消息</span>
                  )}
                </div>
              )}

              {contextLoading && !context && (
                <div className="heq-loading-row">
                  <LoaderCircle className="spin" size={16} />正在读取最近消息…
                </div>
              )}

              {context?.messages.map((message) => (
                <MessageItem
                  key={message.messageId}
                  message={message}
                  isTarget={!manualMode && message.messageId === targetId}
                  onSelect={chooseTarget}
                />
              ))}

              {context && context.messages.length === 0 && (
                <div className="heq-empty">
                  <MessageCircle size={24} />
                  <strong>最近没有可读取的消息</strong>
                  <span>可以在右侧切到「手动输入」，把对方的消息粘进来后继续生成。</span>
                </div>
              )}

              {contextError && !context && !contextLoading && (
                <div className="heq-empty">
                  <AlertCircle size={24} />
                  <strong>上下文暂时不可用</strong>
                  <span>
                    可以稍后重试，或到「设置 → 企业微信」检查数据目录与数据库密钥；
                    也可以在右侧「手动输入」里粘贴一条消息继续生成。
                  </span>
                  <div className="heq-empty-actions">
                    {onNavigate && (
                      <button type="button" className="button button-secondary" onClick={() => onNavigate("settings")}>
                        <Settings2 size={14} />打开设置
                      </button>
                    )}
                    <button type="button" className="button button-secondary" onClick={() => void extractKeys()}>
                      <KeyRound size={14} />提取数据库密钥
                    </button>
                    <button type="button" className="button button-secondary" onClick={refreshContext}>
                      <RefreshCw size={14} />重试读取
                    </button>
                  </div>
                </div>
              )}

              {!selectedChatId && !chatsLoading && (
                <div className="heq-empty">
                  <KeyRound size={24} />
                  <strong>{chatsError ? "无法读取企业微信数据" : "还没有可读取的会话"}</strong>
                  <span>
                    {chatsError
                      || "请确认企业微信正在运行，并在「设置 → 企业微信」选择数据目录、提取数据库密钥；产生聊天记录后最近会话会自动出现。"}
                  </span>
                  <div className="heq-empty-actions">
                    {onNavigate && (
                      <button type="button" className="button button-secondary" onClick={() => onNavigate("settings")}>
                        <Settings2 size={14} />打开设置
                      </button>
                    )}
                    <button type="button" className="button button-secondary" onClick={() => void extractKeys()}>
                      <KeyRound size={14} />提取数据库密钥
                    </button>
                    <button type="button" className="button button-secondary" onClick={() => void refreshAll()} disabled={refreshing}>
                      <RefreshCw size={14} />重试读取
                    </button>
                  </div>
                </div>
              )}
            </div>

            {jumpToLatestVisible && (
              <button type="button" className="heq-jump-latest" onClick={jumpToLatest}>
                <ArrowDown size={13} />跳到最新
              </button>
            )}
          </div>
        </section>

        <aside className="heq-panel heq-reply" aria-label="生成高情商回复">
          <header className="heq-panel-head">
            <div className="heq-panel-heading">
              <span className="eyebrow">REPLY</span>
              <h2>生成回复</h2>
              <small>先选待回复消息，再生成三版措辞</small>
            </div>
            {result && (
              <span className={clsx("heq-badge", result.fallback ? "is-warning" : "is-ai")}>
                {result.fallback ? "规则版" : "AI 生成"}
              </span>
            )}
          </header>

          <div className="heq-reply-body">
            <section className="heq-target-box" aria-label="待回复消息">
              <div className="heq-target-head">
                <span className="heq-target-label">待回复消息</span>
                {manualMode ? (
                  <button
                    type="button"
                    className="heq-text-button"
                    onClick={() => setManualMode(false)}
                    disabled={!targetMessage}
                  >
                    改用上下文消息
                  </button>
                ) : targetMessage ? (
                  <button type="button" className="heq-text-button" onClick={() => setManualMode(true)}>
                    手动输入
                  </button>
                ) : null}
              </div>

              {manualMode ? (
                <textarea
                  className="textarea heq-manual-input"
                  value={manualText}
                  onChange={(event) => setManualText(event.target.value)}
                  onKeyDown={onManualKeyDown}
                  placeholder="把对方的消息粘贴到这里，例如：这个需求怎么又延期了？上周不是说能上吗？"
                  aria-label="手动粘贴的待回复内容"
                />
              ) : targetMessage ? (
                <>
                  <div className="heq-target-meta">
                    <strong>{senderDisplayName(targetMessage.senderName, targetMessage.senderId)}</strong>
                    <time>{formatMessageClock(targetMessage.sendTime)}</time>
                  </div>
                  <p className="heq-target-text">{messageBodyText(targetMessage)}</p>
                  <small className="heq-hint">点击左侧任意消息可切换待回复对象。</small>
                </>
              ) : (
                <div className="heq-target-empty">
                  <span>还没有选择待回复消息。可以点左侧消息，或直接粘贴一条。</span>
                  <button type="button" className="button button-secondary" onClick={() => setManualMode(true)}>
                    <Clipboard size={14} />手动粘贴一条消息
                  </button>
                </div>
              )}
            </section>

            <Field
              label="对方称呼 / 身份（可选）"
              hint="默认使用发送人姓名，可改成更贴切的称呼，只影响生成措辞。"
            >
              <Input
                value={recipient}
                onChange={(event) => setRecipient(event.target.value)}
                placeholder={targetMessage ? senderDisplayName(targetMessage.senderName, targetMessage.senderId) : "例如：客户张总、王姐"}
                aria-label="对方称呼或身份"
              />
            </Field>

            <div className="heq-generate-row">
              <Button
                className="heq-generate"
                onClick={() => void generate()}
                disabled={!canGenerate}
                aria-busy={generating}
              >
                {generating
                  ? <LoaderCircle className="spin" size={16} />
                  : <Sparkles size={16} />}
                {generating ? "正在解读并起草…" : result ? "重新生成" : "生成高情商回复"}
              </Button>
              {manualMode && (
                <span className="heq-generate-hint"><kbd>Ctrl</kbd> + <kbd>Enter</kbd> 也可生成</span>
              )}
              <span className="heq-hint">只复制文案，不会发送；生成前会附带当前会话上下文；图片会在生成时交给模型识别。</span>
            </div>

            {generateError && (
              <div className="heq-inline-error" role="alert">
                <AlertCircle size={15} />
                <span>{generateError}</span>
                <button type="button" onClick={() => void generate()} disabled={generating}>重试</button>
              </div>
            )}

            {generating && !result && (
              <div className="heq-generating">
                <LoaderCircle className="spin" size={16} />
                <span>正在结合会话上下文与图片起草三种风格…</span>
              </div>
            )}

            {result && (
              <div className="heq-result" ref={resultRef}>
                {result.images && result.images.attached > 0 && (
                  <p className="heq-image-note">
                    <ImageIcon size={13} />
                    <span>已附带 {result.images.attached} 张聊天图片给模型识别。</span>
                  </p>
                )}

                {result.fallback && (
                  <div className="heq-fallback">
                    <AlertTriangle size={15} />
                    <span>AI 暂时不可用，已返回<strong>规则版</strong>通用措辞；可以直接修改后复制，稍后再试可能更贴合语境。</span>
                  </div>
                )}

                <section className="heq-section" aria-label="潜台词解读">
                  <h3 className="heq-section-title"><Sparkles size={13} />潜台词解读</h3>
                  {result.interpretation.summary ? (
                    <p className="heq-summary">{result.interpretation.summary}</p>
                  ) : (
                    <p className="heq-muted">没有识别到明显的潜台词，按字面意思理解即可。</p>
                  )}
                  {result.interpretation.signals.length > 0 && (
                    <ul className="heq-signals">
                      {result.interpretation.signals.map((signal, index) => (
                        <li key={`${signal.label}-${index}`}>
                          <strong>{signal.label}</strong>
                          {signal.detail && <span>{signal.detail}</span>}
                        </li>
                      ))}
                    </ul>
                  )}
                </section>

                <section className="heq-section" aria-label="三版回复">
                  <h3 className="heq-section-title"><Clipboard size={13} />三版回复</h3>
                  <div className="heq-style-grid">
                    {result.styles.map((style) => (
                      <article className="heq-style-card" key={style.id}>
                        <div className="heq-style-head">
                          <h4>{eqStyleLabel(style.id)}</h4>
                          <span className="heq-style-count">{(drafts[style.id] ?? "").length} 字</span>
                        </div>
                        <textarea
                          className="textarea heq-style-textarea"
                          value={drafts[style.id] ?? ""}
                          onChange={(event) => updateDraft(style.id, event.target.value)}
                          aria-label={`${eqStyleLabel(style.id)}版回复`}
                        />
                        <div className="heq-style-actions">
                          <Button variant="secondary" onClick={() => void copyStyle(style.id)}>
                            {copiedStyle === style.id ? <Check size={14} /> : <Clipboard size={14} />}
                            {copiedStyle === style.id ? "已复制" : "复制"}
                          </Button>
                        </div>
                      </article>
                    ))}
                  </div>
                </section>

                <section className="heq-section" aria-label="发送前检查">
                  <h3 className="heq-section-title"><ShieldCheck size={13} />发送前检查</h3>
                  {result.check.risks.length > 0 && (
                    <ul className="heq-checklist">
                      {result.check.risks.map((risk, index) => (
                        <li key={`risk-${index}`} className="heq-check-risk">{risk}</li>
                      ))}
                    </ul>
                  )}
                  {result.check.suggestions.length > 0 && (
                    <ul className="heq-checklist">
                      {result.check.suggestions.map((suggestion, index) => (
                        <li key={`suggestion-${index}`} className="heq-check-suggestion">{suggestion}</li>
                      ))}
                    </ul>
                  )}
                  {result.check.risks.length === 0 && result.check.suggestions.length === 0 && (
                    <p className="heq-muted">没有发现明显的语气风险，发送前再确认一次称呼和承诺即可。</p>
                  )}
                </section>
              </div>
            )}

            {!result && !generating && (
              <div className="heq-result-empty">
                <MessageCircle size={18} />
                <div>
                  <strong>生成后会显示在这里</strong>
                  <p>潜台词解读、稳重 / 亲和 / 简短三版可编辑回复，以及发送前检查。</p>
                </div>
              </div>
            )}

            <p className="heq-safety-note">
              <ShieldCheck size={13} />
              只复制到剪贴板，不会向企业微信发送消息，也不会模拟键盘。
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* subcomponents                                                       */
/* ------------------------------------------------------------------ */

function ChatListItem({
  chat,
  active,
  onSelect,
}: {
  chat: WecomRecentChat;
  active: boolean;
  onSelect: (conversationId: string) => void;
}) {
  const displayName = chatDisplayName(chat);
  return (
    <li>
      <button
        type="button"
        className={clsx("heq-chat", active && "is-active", chat.isGroup && "is-group")}
        aria-pressed={active}
        title={`${chatTypeLabel(chat.isGroup)} · ${displayName}`}
        onClick={() => onSelect(chat.conversationId)}
      >
        <span className="heq-chat-avatar" aria-hidden="true">
          {chat.isGroup ? <Users size={16} /> : <User size={16} />}
        </span>
        <span className="heq-chat-body">
          <span className="heq-chat-name">{displayName}</span>
          <span className="heq-chat-meta">
            <span className={clsx("heq-badge", chat.isGroup ? "is-group" : "is-direct")}>
              {chatTypeLabel(chat.isGroup)}
            </span>
            <time className="heq-chat-time">{formatChatTime(chat.lastMessageTime)}</time>
          </span>
        </span>
      </button>
    </li>
  );
}

function MessageItem({
  message,
  isTarget,
  onSelect,
}: {
  message: WecomContextMessage;
  isTarget: boolean;
  onSelect: (message: WecomContextMessage) => void;
}) {
  const imageCount = Math.max(message.imageCount, message.images.length);
  const displayText = messageDisplayText(message);
  const [expanded, setExpanded] = useState(false);
  const collapsible = shouldCollapseMessage(displayText);
  return (
    <article className={clsx("heq-message", isTarget && "is-target", expanded && "is-expanded")}>
      <button
        type="button"
        className="heq-message-select"
        aria-pressed={isTarget}
        title={isTarget ? "当前待回复消息" : "设为待回复对象"}
        onClick={() => onSelect(message)}
      >
        <span className="heq-message-meta">
          <strong>{senderDisplayName(message.senderName, message.senderId)}</strong>
          <time>{formatMessageClock(message.sendTime)}</time>
          {imageCount > 0 && (
            <span className="heq-message-tag"><ImageIcon size={12} />图片 ×{imageCount}</span>
          )}
          {isTarget && <span className="heq-target-flag">待回复</span>}
        </span>
        {displayText && (!collapsible || !expanded) && (
          <span className={clsx("heq-message-text", collapsible && "is-clamped")}>{displayText}</span>
        )}
        {!displayText && imageCount === 0 && (
          <span className="heq-message-text heq-message-muted">[非文本消息]</span>
        )}
      </button>

      {imageCount > 0 && (
        <div className="heq-image-note">
          <ImageIcon size={12} />
          <span>生成回复时会把 {imageCount} 张图片交给模型识别</span>
        </div>
      )}

      {collapsible && (
        <button
          type="button"
          className="heq-message-expand"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起" : "展开全文"}
        </button>
      )}
      {collapsible && expanded && (
        <p className="heq-message-fulltext">{displayText}</p>
      )}
    </article>
  );
}

export default HighEqReplyPage;

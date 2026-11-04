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
  Clock,
  Image as ImageIcon,
  KeyRound,
  LoaderCircle,
  MessageCircle,
  PencilLine,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Trash2,
  User,
  Users,
  X,
} from "lucide-react";
import clsx from "clsx";
import { toast } from "sonner";

import { Button, Field, Input } from "../../components/ui";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Overlay } from "../../components/Overlay";
import { bridge } from "../../lib/bridge";
import { copyToClipboard } from "../../lib/clipboard";
import { errorHasCode, toUserErrorMessage } from "../../lib/errors";
import { createCommand, createQuery } from "../../lib/replyRuntimeUi";
import type { AppConfig } from "../../types";
import {
  CHAT_ALIAS_MAX_LENGTH,
  CHAT_CATALOG_LIMIT,
  CONTEXT_PAGE_SIZE,
  CONTEXT_TEXT_MAX_CHARS,
  CONTEXT_WINDOW_SECONDS,
  DEFAULT_PROMPT_LABEL,
  POLL_INTERVAL_MS,
  PROMPT_DELETED_HINT,
  PROMPT_NOTE_MAX_LENGTH,
  RECENT_CHAT_LIMIT,
  canGenerateReply,
  catalogSelectableIds,
  chatDisplayName,
  chatTypeLabel,
  chatsFingerprint,
  collectContextImageIds,
  collectReplyImageIds,
  composeContext,
  contextFingerprint,
  defaultChatId,
  defaultMessageSelection,
  eqPromptOptions,
  eqReplyPayload,
  eqStyleLabel,
  fallbackReasonCopy,
  fallbackReasonNeedsSettings,
  filterCatalogChats,
  firstAddedChatId,
  focusMessagesPayload,
  formatChatTime,
  formatDurationMs,
  formatMessageClock,
  imageIdsForPayload,
  isNearBottom,
  isPromptMissing,
  mergeContextMessages,
  messageDisplayText,
  nextChatIdAfterRemoval,
  normalizeChatCatalog,
  normalizeChatList,
  normalizeContext,
  normalizeEqResult,
  promptNoteValue,
  readCachedContext,
  removeSelectedMessage,
  replyDrafts,
  selectionFingerprint,
  selectionRecipient,
  selectionText,
  senderDisplayName,
  shouldCollapseMessage,
  shouldLoadOlder,
  shouldPoll,
  toggleMessageSelection,
  writeCachedContext,
  type ChatContextCache,
  type EqReplyResult,
  type SelectedMessageSnapshot,
  type WecomCatalogChat,
  type WecomChatContext,
  type WecomContextMessage,
  type WecomRecentChat,
} from "./highEqReply.logic";
import "./highEqReply.css";

export interface HighEqReplyPageProps {
  /** Bootstrap configuration; prompt-library entries feed the per-chat picker. */
  config: AppConfig;
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

/** Curated-chat write command body (revision is passed separately). */
type ChatCommandBody = { kind: string; [key: string]: unknown };

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
export function HighEqReplyPage({ config, active = true, onNavigate }: HighEqReplyPageProps) {
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
  const [selection, setSelection] = useState<SelectedMessageSnapshot[]>([]);
  const [manualMode, setManualMode] = useState(false);
  const [manualText, setManualText] = useState("");
  const [recipient, setRecipient] = useState("");
  const [result, setResult] = useState<EqReplyResult | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState("");
  const [copiedStyle, setCopiedStyle] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [imageUnderstanding, setImageUnderstanding] = useState(true);
  const [promptNoteDraft, setPromptNoteDraft] = useState("");
  const [promptNoteExpanded, setPromptNoteExpanded] = useState(false);
  const [promptSaving, setPromptSaving] = useState(false);
  const [resultImageMode, setResultImageMode] = useState(true);
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [catalogChats, setCatalogChats] = useState<WecomCatalogChat[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [catalogSearch, setCatalogSearch] = useState("");
  const [catalogSelected, setCatalogSelected] = useState<string[]>([]);
  const [catalogSaving, setCatalogSaving] = useState(false);
  const [renameTarget, setRenameTarget] = useState<WecomRecentChat | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameSaving, setRenameSaving] = useState(false);
  const [removeTarget, setRemoveTarget] = useState<WecomRecentChat | null>(null);
  const [removing, setRemoving] = useState(false);

  const selectedChatIdRef = useRef("");
  const selectionRef = useRef<SelectedMessageSnapshot[]>([]);
  const selectionTouchedRef = useRef(false);
  const chatsRef = useRef<WecomRecentChat[]>([]);
  const revisionRef = useRef(0);
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

  const applySelection = useCallback((next: SelectedMessageSnapshot[]) => {
    selectionRef.current = next;
    setSelection(next);
  }, []);

  const applyContext = useCallback((next: WecomChatContext) => {
    contextRef.current = next;
    setContext(next);
  }, []);

  /** Drops everything that only made sense for the previous conversation. */
  const resetConversationState = useCallback(() => {
    selectionRef.current = [];
    setSelection([]);
    selectionTouchedRef.current = false;
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
  }, []);

  /**
   * Applies the conversation default until the user interacts: direct chats
   * select the newest message, groups start with an empty selection.
   */
  const syncDefaultSelection = useCallback((
    messages: WecomContextMessage[],
    isGroup: boolean,
  ) => {
    if (selectionTouchedRef.current) return;
    const next = defaultMessageSelection(messages, isGroup);
    if (selectionFingerprint(selectionRef.current) === selectionFingerprint(next)) return;
    applySelection(next);
    const nextRecipient = selectionRecipient(next);
    if (nextRecipient) setRecipient(nextRecipient);
    else if (!isGroup) setRecipient("");
  }, [applySelection]);

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
      syncDefaultSelection(cached.messages, cached.isGroup);
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
        syncDefaultSelection(mergedMessages, next.isGroup);
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
  }, [applyContext, queryContextPage, syncDefaultSelection]);

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
      const payload = normalizeChatList(raw);
      const next = payload.chats;
      chatsRef.current = next;
      revisionRef.current = payload.revision;
      setChats((current) => (
        chatsFingerprint(current) === chatsFingerprint(next) ? current : next
      ));
      setChatsError("");
      const currentId = selectedChatIdRef.current;
      if (!next.some((chat) => chat.conversationId === currentId)) {
        selectChatId(defaultChatId(next));
      }
      return payload;
    } catch (error) {
      setChatsError(toUserErrorMessage(error, "无法读取企业微信会话，请稍后重试。"));
      return { chats: [], revision: revisionRef.current };
    } finally {
      if (!quiet) setChatsLoading(false);
    }
  }, [selectChatId]);

  const refreshAll = useCallback(async () => {
    setRefreshing(true);
    try {
      const payload = await loadChats({ quiet: true, refresh: true });
      const nextChats = payload.chats;
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

  /* ---------------- curated chat list (v4.6.5) ---------------- */

  /** Executes a curated-chat command, retrying once after a revision conflict. */
  const runChatCommand = useCallback(async (body: ChatCommandBody) => {
    const execute = async (revision: number): Promise<number> => {
      const result = await bridge.replyRuntimeExecute<Record<string, unknown>>(
        createCommand(body, revision),
      );
      const nextRevision = Number(result?.revision);
      return Number.isFinite(nextRevision) && nextRevision >= 0
        ? Math.floor(nextRevision)
        : revision;
    };
    try {
      revisionRef.current = await execute(revisionRef.current);
    } catch (error) {
      if (!errorHasCode(error, "REVISION_CONFLICT")) throw error;
      const refreshed = await loadChats({ quiet: true, refresh: true });
      revisionRef.current = refreshed.revision;
      revisionRef.current = await execute(refreshed.revision);
    }
  }, [loadChats]);

  const loadCatalog = useCallback(async () => {
    setCatalogLoading(true);
    setCatalogError("");
    try {
      const raw = await bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({
        kind: "chat.catalog",
        limit: CHAT_CATALOG_LIMIT,
      }));
      const payload = normalizeChatCatalog(raw);
      setCatalogChats(payload.chats);
      revisionRef.current = payload.revision;
    } catch (error) {
      setCatalogError(toUserErrorMessage(error, "无法读取会话目录，请稍后重试。"));
    } finally {
      setCatalogLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!addDialogOpen) return;
    void loadCatalog();
  }, [addDialogOpen, loadCatalog]);

  const openAddDialog = useCallback(() => {
    setCatalogSearch("");
    setCatalogSelected([]);
    setCatalogError("");
    setAddDialogOpen(true);
  }, []);

  const closeAddDialog = () => {
    if (catalogSaving) return;
    setAddDialogOpen(false);
  };

  const toggleCatalogChat = (conversationId: string) => {
    setCatalogSelected((current) => (
      current.includes(conversationId)
        ? current.filter((id) => id !== conversationId)
        : [...current, conversationId]
    ));
  };

  const filteredCatalog = useMemo(
    () => filterCatalogChats(catalogChats, catalogSearch),
    [catalogChats, catalogSearch],
  );

  const selectableCatalogIds = useMemo(
    () => catalogSelectableIds(filteredCatalog),
    [filteredCatalog],
  );

  const allFilteredSelected = selectableCatalogIds.length > 0
    && selectableCatalogIds.every((id) => catalogSelected.includes(id));

  const toggleCatalogAll = () => {
    if (allFilteredSelected) {
      setCatalogSelected((current) => current.filter((id) => !selectableCatalogIds.includes(id)));
      return;
    }
    setCatalogSelected((current) => {
      const next = new Set(current);
      for (const id of selectableCatalogIds) next.add(id);
      return [...next];
    });
  };

  const addCatalogChats = async () => {
    const selected = catalogSelected.filter((id) => (
      catalogChats.some((chat) => chat.conversationId === id && !chat.added)
    ));
    if (selected.length === 0) return;
    setCatalogSaving(true);
    try {
      await runChatCommand({
        kind: "chat.chats.add",
        chats: selected.map((conversationId) => ({ conversationId })),
      });
      const previousChats = chatsRef.current;
      const payload = await loadChats({ quiet: true, refresh: true });
      setCatalogChats((current) => current.map((chat) => (
        selected.includes(chat.conversationId) ? { ...chat, added: true } : chat
      )));
      setAddDialogOpen(false);
      toast.success(`已添加 ${selected.length} 个会话`);
      const nextId = firstAddedChatId(previousChats, payload.chats);
      if (nextId) selectChat(nextId);
    } catch (error) {
      toast.error("添加会话失败", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
    } finally {
      setCatalogSaving(false);
    }
  };

  const openRenameDialog = (chat: WecomRecentChat) => {
    setRenameTarget(chat);
    setRenameValue(chat.alias ?? "");
  };

  const openRemoveDialog = (chat: WecomRecentChat) => {
    setRemoveTarget(chat);
  };

  const saveRename = async () => {
    const target = renameTarget;
    if (!target || renameSaving) return;
    const alias = renameValue.trim().slice(0, CHAT_ALIAS_MAX_LENGTH);
    setRenameSaving(true);
    try {
      await runChatCommand({
        kind: "chat.chats.rename",
        conversationId: target.conversationId,
        alias,
      });
      setRenameTarget(null);
      setCatalogChats((current) => current.map((chat) => (
        chat.conversationId === target.conversationId
          ? { ...chat, alias: alias || undefined }
          : chat
      )));
      toast.success(alias ? "本地别名已保存" : "已清除本地别名");
      await loadChats({ quiet: true, refresh: true });
    } catch (error) {
      toast.error("保存别名失败", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
    } finally {
      setRenameSaving(false);
    }
  };

  const confirmRemoveChat = async () => {
    const target = removeTarget;
    if (!target || removing) return;
    setRemoving(true);
    try {
      await runChatCommand({
        kind: "chat.chats.remove",
        conversationIds: [target.conversationId],
      });
      const nextId = nextChatIdAfterRemoval(
        chatsRef.current,
        [target.conversationId],
        selectedChatIdRef.current,
      );
      setRemoveTarget(null);
      const payload = await loadChats({ quiet: true, refresh: true });
      if (nextId && payload.chats.some((chat) => chat.conversationId === nextId)) {
        selectChat(nextId);
      }
      toast.success("已从我的会话中移除");
    } catch (error) {
      toast.error("移除会话失败", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
    } finally {
      setRemoving(false);
    }
  };

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
  const promptOptions = useMemo(
    () => eqPromptOptions(config.prompts?.items),
    [config.prompts],
  );
  const boundPromptId = selectedChat?.promptId ?? "";
  const boundPromptNote = selectedChat?.promptNote ?? "";
  const promptMissing = isPromptMissing(promptOptions, boundPromptId);
  const promptNoteDirty = promptNoteValue(promptNoteDraft) !== boundPromptNote;

  // Follow the selected chat's saved role note; a poll returning the same note
  // must not overwrite what the user is currently typing.
  useEffect(() => {
    setPromptNoteDraft(selectedChat?.promptNote ?? "");
  }, [selectedChatId, selectedChat?.promptNote]);

  const isGroupChat = context?.isGroup ?? selectedChat?.isGroup ?? false;
  const selectionTextValue = useMemo(() => selectionText(selection), [selection]);
  const focusPayload = useMemo(() => focusMessagesPayload(selection), [selection]);
  const composedContext = useMemo(
    () => composeContext(context?.messages ?? [], {}, {
      excludeMessageIds: manualMode ? [] : selection.map((item) => item.messageId),
      maxChars: CONTEXT_TEXT_MAX_CHARS,
    }),
    [context, manualMode, selection],
  );
  const contextText = composedContext.text;
  const contextTruncated = composedContext.truncated;
  const candidateImageIds = useMemo(
    () => (manualMode
      ? collectContextImageIds(context?.messages ?? [])
      : collectReplyImageIds(context?.messages ?? [], selection)),
    [context, manualMode, selection],
  );
  const imageMessageIds = useMemo(
    () => imageIdsForPayload(imageUnderstanding, candidateImageIds),
    [candidateImageIds, imageUnderstanding],
  );
  const canGenerate = canGenerateReply({
    manualMode,
    manualText,
    selection,
    contextMessageCount: context?.messages.length ?? 0,
  }) && !generating;
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
  const resultDurationLabel = result ? formatDurationMs(result.timings?.totalMs) : "";
  const resultFallbackReason = result?.ai?.fallbackReason ?? result?.fallbackReason;
  const resultPromptFallback = Boolean(result?.ai?.promptFallback);
  const resultImageCopy = result
    ? (resultImageMode
      ? [
          (result.images?.attached ?? 0) > 0
            ? `已附带 ${result.images?.attached} 张聊天图片给模型识别`
            : "",
          (result.images?.skipped ?? 0) > 0
            ? `忽略 ${result.images?.skipped} 张（超出数量或大小限制）`
            : "",
        ].filter(Boolean).join("；")
      : "本次只发送文字，未附带图片")
    : "";

  const toggleSelection = (message: WecomContextMessage) => {
    if (manualMode) setManualMode(false);
    selectionTouchedRef.current = true;
    setGenerateError("");
    const next = toggleMessageSelection(selectionRef.current, message);
    applySelection(next);
    if (next.length === 0) setRecipient("");
    else if (next.length === 1) setRecipient(next[0].senderName);
    setResult(null);
    setDrafts({});
  };

  const removeSelectionItem = (messageId: string) => {
    selectionTouchedRef.current = true;
    setGenerateError("");
    const next = removeSelectedMessage(selectionRef.current, messageId);
    applySelection(next);
    if (next.length === 0) setRecipient("");
    setResult(null);
    setDrafts({});
  };

  const clearSelection = () => {
    selectionTouchedRef.current = true;
    setGenerateError("");
    applySelection([]);
    setRecipient("");
    setResult(null);
    setDrafts({});
  };

  /* ---------------- per-chat prompt binding (v4.6.6) ---------------- */

  const updateChatPromptLocally = (
    conversationId: string,
    patch: { promptId?: string; promptNote?: string },
  ) => {
    const apply = (chat: WecomRecentChat): WecomRecentChat => (
      chat.conversationId === conversationId ? { ...chat, ...patch } : chat
    );
    setChats((current) => current.map(apply));
    chatsRef.current = chatsRef.current.map(apply);
  };

  const persistPromptBinding = async (
    patch: { promptId?: string; promptNote?: string },
  ): Promise<boolean> => {
    const chat = selectedChat;
    if (!chat) return false;
    setPromptSaving(true);
    try {
      await runChatCommand({
        kind: "chat.prompt.set",
        conversationId: chat.conversationId,
        ...patch,
      });
      updateChatPromptLocally(chat.conversationId, patch);
      return true;
    } catch (error) {
      toast.error("保存本群提示词失败", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
      return false;
    } finally {
      setPromptSaving(false);
    }
  };

  const changePrompt = async (nextPromptId: string) => {
    const chat = selectedChat;
    if (!chat || promptSaving) return;
    const previous = chat.promptId ?? "";
    if (nextPromptId === previous) return;
    // Optimistic: the select responds immediately and reverts if the command fails.
    updateChatPromptLocally(chat.conversationId, { promptId: nextPromptId });
    const saved = await persistPromptBinding({ promptId: nextPromptId });
    if (!saved) updateChatPromptLocally(chat.conversationId, { promptId: previous });
  };

  const savePromptNote = async () => {
    const chat = selectedChat;
    if (!chat || promptSaving || !promptNoteDirty) return;
    const note = promptNoteValue(promptNoteDraft);
    const saved = await persistPromptBinding({ promptNote: note });
    if (saved) {
      setPromptNoteDraft(note);
      toast.success(note ? "本群角色补充已保存" : "已清除本群角色补充");
    }
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
    const manualTextValue = manualText.trim();
    if (manualMode && !manualTextValue) {
      setGenerateError("请先粘贴对方的消息，再生成回复。");
      return;
    }
    if (!manualMode && !canGenerateReply({
      manualMode: false,
      manualText: "",
      selection,
      contextMessageCount: context?.messages.length ?? 0,
    })) {
      setGenerateError("当前会话没有可读取的上下文，请先选择消息或手动粘贴一条。");
      return;
    }
    const text = manualMode ? manualTextValue : selectionTextValue;
    const trimmedRecipient = recipient.trim()
      || (!manualMode ? selectionRecipient(selection) : "");
    setGenerating(true);
    setGenerateError("");
    try {
      const raw = await bridge.assistantAction<unknown>(
        "eq_reply",
        eqReplyPayload({
          text,
          context: contextText,
          recipient: trimmedRecipient,
          conversationId: context?.conversationId ?? "",
          promptId: boundPromptId,
          promptNote: boundPromptNote,
          imageMessageIds,
          focusMessages: manualMode ? undefined : focusPayload,
        }),
      );
      const normalized = normalizeEqResult(raw);
      if (normalized.styles.length === 0) {
        setGenerateError("这次没有生成可用的回复草稿，可以稍后重试，或把待回复内容缩短一些。");
        return;
      }
      setResultImageMode(imageUnderstanding);
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
          <p>只读取「我的会话」里添加的群聊/私聊，生成时结合聊天图片；先看潜台词，再挑一版回复。只复制，不会替你发送。</p>
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
        <aside className="heq-panel heq-rail" aria-label="我的会话">
          <header className="heq-panel-head">
            <div className="heq-panel-heading">
              <span className="eyebrow">MY CHATS</span>
              <h2>我的会话</h2>
              <small>仅显示已添加会话</small>
            </div>
            <div className="heq-panel-actions">
              <button
                type="button"
                className="heq-icon-button"
                title="添加会话"
                aria-label="添加会话"
                onClick={openAddDialog}
              >
                <Plus size={14} />
              </button>
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
              <span>正在读取我的会话…</span>
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
            <div className="heq-rail-state heq-rail-empty">
              <Users size={20} />
              <strong>还没有添加会话</strong>
              <span>只同步你添加的群聊/私聊；添加后才会读取上下文。</span>
              <button type="button" className="button button-secondary" onClick={openAddDialog}>
                <Plus size={14} />添加会话
              </button>
            </div>
          ) : (
            <ul className="heq-rail-list">
              {chats.map((chat) => (
                <ChatListItem
                  key={chat.conversationId}
                  chat={chat}
                  active={chat.conversationId === selectedChatId}
                  onSelect={selectChat}
                  onRename={openRenameDialog}
                  onRemove={openRemoveDialog}
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
                  selected={!manualMode && selection.some((item) => item.messageId === message.messageId)}
                  imageUnderstanding={imageUnderstanding}
                  onToggle={toggleSelection}
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
                  {chatsError ? <AlertCircle size={24} /> : <Users size={24} />}
                  <strong>{chatsError ? "无法读取我的会话" : "还没有添加会话"}</strong>
                  <span>
                    {chatsError
                      || "从最近会话中添加常看的群聊/私聊；添加后才会读取上下文并参与生成。"}
                  </span>
                  <div className="heq-empty-actions">
                    {!chatsError && (
                      <button type="button" className="button button-secondary" onClick={openAddDialog}>
                        <Plus size={14} />添加会话
                      </button>
                    )}
                    {chatsError && onNavigate && (
                      <button type="button" className="button button-secondary" onClick={() => onNavigate("settings")}>
                        <Settings2 size={14} />打开设置
                      </button>
                    )}
                    {chatsError && (
                      <button type="button" className="button button-secondary" onClick={() => void extractKeys()}>
                        <KeyRound size={14} />提取数据库密钥
                      </button>
                    )}
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
              <small>可结合上下文生成，也可点选多条消息</small>
            </div>
            {result && (
              <span className={clsx("heq-badge", result.fallback ? "is-warning" : "is-ai")}>
                {result.fallback ? "本地模板" : "AI 生成"}
              </span>
            )}
          </header>

          <div className="heq-reply-body">
            {selectedChat && (
              <section className="heq-prompt-box" aria-label="本群提示词与角色补充">
                <div className="heq-prompt-head">
                  <span className="heq-prompt-label">本群提示词</span>
                  {onNavigate && (
                    <button
                      type="button"
                      className="heq-text-button"
                      onClick={() => onNavigate("strategy-prompts")}
                    >
                      提示词库
                    </button>
                  )}
                </div>
                <select
                  className="select heq-prompt-select"
                  value={boundPromptId}
                  onChange={(event) => void changePrompt(event.target.value)}
                  disabled={promptSaving}
                  aria-label="本群使用的提示词"
                >
                  <option value="">{DEFAULT_PROMPT_LABEL}</option>
                  {promptMissing && (
                    <option value={boundPromptId} disabled>
                      {PROMPT_DELETED_HINT}
                    </option>
                  )}
                  {promptOptions.map((option) => (
                    <option key={option.id} value={option.id}>{option.name}</option>
                  ))}
                </select>
                {promptOptions.length === 0 && (
                  <small className="heq-hint">提示词库还没有高情商回复提示词，将使用内置默认。</small>
                )}
                {promptMissing && (
                  <p className="heq-hint-note">
                    <AlertTriangle size={13} />
                    <span>{PROMPT_DELETED_HINT}</span>
                  </p>
                )}
                <span className="heq-hint">只影响这个群；在提示词库维护角色模板。</span>
                <div className="heq-prompt-note">
                  <button
                    type="button"
                    className="heq-prompt-note-toggle"
                    aria-expanded={promptNoteExpanded}
                    onClick={() => setPromptNoteExpanded((value) => !value)}
                  >
                    <span>本群角色补充</span>
                    <span className="heq-prompt-count">
                      {boundPromptNote ? "已填写" : "可选"} · {promptNoteDraft.length}/{PROMPT_NOTE_MAX_LENGTH}
                      <ChevronDown size={12} className="heq-chevron" />
                    </span>
                  </button>
                  {promptNoteExpanded && (
                    <>
                      <textarea
                        className="textarea heq-prompt-note-input"
                        value={promptNoteDraft}
                        maxLength={PROMPT_NOTE_MAX_LENGTH}
                        onChange={(event) => setPromptNoteDraft(
                          event.target.value.slice(0, PROMPT_NOTE_MAX_LENGTH),
                        )}
                        placeholder="可选：说明你在这个群里的角色、口吻或要守住的分寸…"
                        aria-label="本群角色补充"
                        rows={2}
                      />
                      <div className="heq-prompt-actions">
                        <Button
                          variant="secondary"
                          onClick={() => void savePromptNote()}
                          disabled={!promptNoteDirty || promptSaving}
                          aria-busy={promptSaving}
                        >
                          {promptSaving ? <LoaderCircle className="spin" size={13} /> : null}
                          保存补充
                        </Button>
                      </div>
                    </>
                  )}
                </div>
              </section>
            )}

            <section className="heq-target-box" aria-label="待回复消息">
              <div className="heq-target-head">
                <span className="heq-target-label">
                  {manualMode
                    ? "手动输入"
                    : selection.length > 0
                      ? `已选 ${selection.length} 条消息`
                      : "待回复消息"}
                </span>
                <span className="heq-target-head-actions">
                  {!manualMode && selection.length > 0 && (
                    <button type="button" className="heq-text-button" onClick={clearSelection}>
                      清空
                    </button>
                  )}
                  {manualMode ? (
                    <button type="button" className="heq-text-button" onClick={() => setManualMode(false)}>
                      改用上下文消息
                    </button>
                  ) : (
                    <button type="button" className="heq-text-button" onClick={() => setManualMode(true)}>
                      手动输入
                    </button>
                  )}
                </span>
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
              ) : selection.length > 0 ? (
                <ul className="heq-selection-list">
                  {selection.map((item) => (
                    <li key={item.messageId} className="heq-selection-item">
                      <div className="heq-selection-meta">
                        <strong>{item.senderName}</strong>
                        <time>{formatMessageClock(item.sendTime)}</time>
                      </div>
                      <p className="heq-selection-text">{item.text}</p>
                      <button
                        type="button"
                        className="heq-selection-remove"
                        title="移除这条待回复消息"
                        aria-label={`移除${item.senderName}的这条消息`}
                        onClick={() => removeSelectionItem(item.messageId)}
                      >
                        <X size={12} />
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="heq-target-empty">
                  <span>
                    {isGroupChat
                      ? context?.messages.length
                        ? "未选择消息，将结合群聊上下文生成；点击左侧消息可加选。"
                        : "群聊当前没有可读取的上下文，请手动粘贴一条消息。"
                      : context?.messages.length
                        ? "未选择消息，将结合当前会话上下文生成。"
                        : "当前没有可读取的上下文，请手动粘贴一条消息。"}
                  </span>
                  <button type="button" className="button button-secondary" onClick={() => setManualMode(true)}>
                    <Clipboard size={14} />手动粘贴一条消息
                  </button>
                </div>
              )}

              {!manualMode && selection.length > 0 && (
                <small className="heq-hint">继续点击左侧消息可加选或取消；生成会综合回应已选消息。</small>
              )}
            </section>

            <Field
              label="对方称呼 / 身份（可选）"
              hint="默认使用选中消息的发送人，可修改。"
            >
              <Input
                value={recipient}
                onChange={(event) => setRecipient(event.target.value)}
                placeholder={selectionRecipient(selection) || "例如：客户张总、王姐"}
                aria-label="对方称呼或身份"
              />
            </Field>

            <div className="heq-generate-row">
              <div className="heq-generate-options">
                <label className="heq-image-toggle">
                  <input
                    type="checkbox"
                    checked={imageUnderstanding}
                    onChange={(event) => setImageUnderstanding(event.target.checked)}
                    disabled={generating}
                  />
                  <span>带图片理解</span>
                  <small>
                    {imageUnderstanding
                      ? candidateImageIds.length > 0
                        ? `将附带 ${candidateImageIds.length} 张图片`
                        : "当前没有可附带图片"
                      : "本次只发送文字"}
                  </small>
                </label>
              </div>
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
              {contextTruncated && (
                <p className="heq-hint-note">
                  <AlertTriangle size={13} />
                  <span>上下文已截断，仅发送前 {CONTEXT_TEXT_MAX_CHARS} 字。</span>
                </p>
              )}
              {manualMode && (
                <span className="heq-generate-hint"><kbd>Ctrl</kbd> + <kbd>Enter</kbd> 也可生成</span>
              )}
              <span className="heq-hint">只复制，不会发送。</span>
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
                {(resultDurationLabel || resultPromptFallback) && (
                  <div className="heq-result-summary">
                    {resultDurationLabel && (
                      <span className="heq-result-duration">
                        <Clock size={12} />耗时 {resultDurationLabel}
                      </span>
                    )}
                    {resultPromptFallback && (
                      <span className="heq-badge is-warning">{PROMPT_DELETED_HINT}</span>
                    )}
                  </div>
                )}

                {resultImageCopy && (
                  <p className="heq-image-note">
                    <ImageIcon size={13} />
                    <span>{resultImageCopy}</span>
                  </p>
                )}

                {result.fallback && (
                  <div className="heq-fallback">
                    <AlertTriangle size={15} />
                    <span>{fallbackReasonCopy(resultFallbackReason)}</span>
                    {fallbackReasonNeedsSettings(resultFallbackReason) && onNavigate && (
                      <button
                        type="button"
                        className="heq-fallback-link"
                        onClick={() => onNavigate("settings")}
                      >
                        去设置
                      </button>
                    )}
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

      <Overlay
        open={addDialogOpen}
        onClose={closeAddDialog}
        closeDisabled={catalogSaving}
        size="md"
        eyebrow="CHAT CATALOG"
        title="添加会话"
        subtitle="只同步你添加的群聊/私聊；已添加的会话不会重复。"
        footer={(
          <div className="overlay-footer-actions">
            <Button variant="secondary" onClick={closeAddDialog} disabled={catalogSaving}>取消</Button>
            <Button
              onClick={() => void addCatalogChats()}
              disabled={catalogSelected.length === 0 || catalogSaving}
              aria-busy={catalogSaving}
            >
              {catalogSaving
                ? <LoaderCircle className="spin" size={14} />
                : <Plus size={14} />}
              添加{catalogSelected.length > 0 ? ` ${catalogSelected.length} 个` : ""}
            </Button>
          </div>
        )}
      >
        <div className="heq-catalog">
          <div className="heq-catalog-search">
            <Search size={15} aria-hidden="true" />
            <Input
              autoFocus
              value={catalogSearch}
              onChange={(event) => setCatalogSearch(event.target.value)}
              placeholder="搜索群名、私聊名或会话 ID"
              aria-label="搜索可添加的会话"
            />
          </div>
          <div className="heq-catalog-toolbar">
            <span>共 {filteredCatalog.length} 个 · 已选 {catalogSelected.length} 个</span>
            <button
              type="button"
              className="heq-text-button"
              onClick={toggleCatalogAll}
              disabled={selectableCatalogIds.length === 0}
            >
              {allFilteredSelected ? "取消全选" : "全选当前结果"}
            </button>
          </div>

          {catalogLoading ? (
            <div className="heq-loading-row">
              <LoaderCircle className="spin" size={16} />正在读取会话目录…
            </div>
          ) : catalogError ? (
            <div className="heq-inline-error" role="alert">
              <AlertCircle size={15} />
              <span>{catalogError}</span>
              <button type="button" onClick={() => void loadCatalog()}>重试</button>
            </div>
          ) : filteredCatalog.length === 0 ? (
            <div className="heq-empty">
              <Search size={20} />
              <strong>{catalogChats.length > 0 ? "没有匹配的会话" : "没有可添加的会话"}</strong>
              <span>
                {catalogChats.length > 0
                  ? "换个关键词试试。"
                  : "企业微信里还没有可读取的最近会话。"}
              </span>
            </div>
          ) : (
            <ul className="heq-catalog-list">
              {filteredCatalog.map((chat) => {
                const checked = catalogSelected.includes(chat.conversationId);
                return (
                  <li key={chat.conversationId}>
                    <label
                      className={clsx(
                        "heq-catalog-item",
                        chat.added && "is-added",
                        checked && "is-selected",
                      )}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={chat.added}
                        onChange={() => toggleCatalogChat(chat.conversationId)}
                        aria-label={`${chat.added ? "已添加" : "选择"}${chatDisplayName(chat)}`}
                      />
                      <span className="heq-catalog-body">
                        <span className="heq-catalog-name">{chatDisplayName(chat)}</span>
                        <span className="heq-catalog-meta">
                          <span className={clsx("heq-badge", chat.isGroup ? "is-group" : "is-direct")}>
                            {chatTypeLabel(chat.isGroup)}
                          </span>
                          <time>{formatChatTime(chat.lastMessageTime)}</time>
                        </span>
                      </span>
                      {chat.added && <span className="heq-badge is-muted">已添加</span>}
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </Overlay>

      <Overlay
        open={Boolean(renameTarget)}
        onClose={() => {
          if (!renameSaving) setRenameTarget(null);
        }}
        closeDisabled={renameSaving}
        size="sm"
        eyebrow="LOCAL ALIAS"
        title="重命名会话"
        subtitle="别名只保存在本机，不会写入企业微信。"
        footer={(
          <div className="overlay-footer-actions">
            <Button variant="secondary" onClick={() => setRenameTarget(null)} disabled={renameSaving}>
              取消
            </Button>
            <Button onClick={() => void saveRename()} disabled={renameSaving} aria-busy={renameSaving}>
              {renameSaving ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}
              保存
            </Button>
          </div>
        )}
      >
        <Field
          label="本地别名"
          hint="留空保存会清除别名，恢复显示原会话名称。"
        >
          <Input
            autoFocus
            value={renameValue}
            maxLength={CHAT_ALIAS_MAX_LENGTH}
            onChange={(event) => setRenameValue(event.target.value)}
            placeholder={renameTarget
              ? chatDisplayName({
                conversationId: renameTarget.conversationId,
                displayName: renameTarget.displayName,
                isGroup: renameTarget.isGroup,
              })
              : ""}
            aria-label="会话的本地别名"
          />
        </Field>
      </Overlay>

      {removeTarget && (
        <ConfirmDialog
          request={{
            title: "移除会话？",
            message: `将从「我的会话」中移除「${chatDisplayName(removeTarget)}」，不再读取它的上下文；企业微信聊天记录不会被删除。`,
            confirmLabel: "移除",
            tone: "danger",
            onConfirm: () => void confirmRemoveChat(),
          }}
          onClose={() => setRemoveTarget(null)}
        />
      )}
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
  onRename,
  onRemove,
}: {
  chat: WecomRecentChat;
  active: boolean;
  onSelect: (conversationId: string) => void;
  onRename: (chat: WecomRecentChat) => void;
  onRemove: (chat: WecomRecentChat) => void;
}) {
  const displayName = chatDisplayName(chat);
  const unavailable = chat.available === false;
  return (
    <li className="heq-chat-row">
      <button
        type="button"
        className={clsx(
          "heq-chat",
          active && "is-active",
          chat.isGroup && "is-group",
          unavailable && "is-unavailable",
        )}
        aria-pressed={active}
        title={`${chatTypeLabel(chat.isGroup)} · ${displayName}${unavailable ? " · 会话不可用" : ""}`}
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
            {unavailable && <span className="heq-badge is-warning">不可用</span>}
            <time className="heq-chat-time">{formatChatTime(chat.lastMessageTime)}</time>
          </span>
        </span>
      </button>
      <span className="heq-chat-actions">
        <button
          type="button"
          className="heq-chat-action"
          title="重命名（仅保存在本机）"
          aria-label={`重命名${displayName}`}
          onClick={() => onRename(chat)}
        >
          <PencilLine size={12} />
        </button>
        <button
          type="button"
          className="heq-chat-action is-danger"
          title="从我的会话中移除"
          aria-label={`移除${displayName}`}
          onClick={() => onRemove(chat)}
        >
          <Trash2 size={12} />
        </button>
      </span>
    </li>
  );
}

function MessageItem({
  message,
  selected,
  imageUnderstanding,
  onToggle,
}: {
  message: WecomContextMessage;
  selected: boolean;
  imageUnderstanding: boolean;
  onToggle: (message: WecomContextMessage) => void;
}) {
  const imageCount = Math.max(message.imageCount, message.images.length);
  const displayText = messageDisplayText(message);
  const [expanded, setExpanded] = useState(false);
  const collapsible = shouldCollapseMessage(displayText);
  return (
    <article className={clsx("heq-message", selected && "is-selected", expanded && "is-expanded")}>
      <button
        type="button"
        className="heq-message-select"
        aria-pressed={selected}
        title={selected ? "取消选择这条消息" : "加入待回复消息"}
        onClick={() => onToggle(message)}
      >
        <span className="heq-message-meta">
          <strong>{senderDisplayName(message.senderName, message.senderId)}</strong>
          <time>{formatMessageClock(message.sendTime)}</time>
          {imageCount > 0 && (
            <span className="heq-message-tag"><ImageIcon size={12} />图片 ×{imageCount}</span>
          )}
          {selected && (
            <span className="heq-target-flag"><Check size={10} />已选</span>
          )}
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
          <span>
            {imageUnderstanding
              ? `生成回复时会把 ${imageCount} 张图片交给模型识别`
              : "已关闭图片理解，本次只发送文字"}
          </span>
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

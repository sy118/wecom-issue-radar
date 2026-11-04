/**
 * Pure helpers for the 企业微信 → 高情商回复 page.
 *
 * This module is React-free and bridge-free so the rules that are easy to get
 * wrong can be unit-tested against plain fixtures:
 * - recent-chat normalization, ordering and default selection
 * - read-only chat context normalization (ascending messages, image descriptors)
 * - OCR result merging plus placeholder copy for unreadable images
 * - `[HH:mm] sender: content` context composition (OCR text included)
 * - reply-target keep/fallback rules and poll fingerprints used for de-duping
 * - defensive `eq_reply` normalization (styles / interpretation / check)
 */

/* ------------------------------------------------------------------ */
/* constants                                                           */
/* ------------------------------------------------------------------ */

/** The page refreshes the chat list and the open context every 30 seconds. */
export const POLL_INTERVAL_MS = 30_000;
/** Messages per context page (initial load and each "load older" request). */
export const CONTEXT_PAGE_SIZE = 20;
/** Initial page reads the last six hours… */
export const CONTEXT_WINDOW_SECONDS = 6 * 60 * 60;
export const RECENT_CHAT_LIMIT = 40;
/** Distance from the bottom that still counts as "following the latest". */
export const STICK_TO_BOTTOM_THRESHOLD = 40;
/** Scroll position that triggers loading the previous page. */
export const LOAD_OLDER_THRESHOLD = 24;
/** Re-render a conversation from the renderer cache without waiting for the runtime. */
export const CONTEXT_CACHE_TTL_MS = 5 * 60 * 1000;
/** Long messages are only folded beyond these bounds (chars / lines). */
export const COLLAPSE_MESSAGE_MAX_CHARS = 600;
export const COLLAPSE_MESSAGE_MAX_LINES = 12;
/** Upper bound for one "add chats" catalog request. */
export const CHAT_CATALOG_LIMIT = 300;
/** The model receives at most this many focused messages (v4.6.6: 20 → 10). */
export const FOCUS_MESSAGES_LIMIT = 10;
/** Composed generation context is trimmed to this many characters. */
export const CONTEXT_TEXT_MAX_CHARS = 3000;
/** Local per-chat role note limit (runtime command validates the same bound). */
export const PROMPT_NOTE_MAX_LENGTH = 500;
/** Option label shown when a chat follows the first eq_reply prompt. */
export const DEFAULT_PROMPT_LABEL = "默认（提示词库第一条）";
/** Warning shown when the bound prompt no longer exists in the prompt library. */
export const PROMPT_DELETED_HINT = "提示词已删除，使用默认";
/** Local alias length accepted by the runtime command. */
export const CHAT_ALIAS_MAX_LENGTH = 40;
/** `chat.ocr` accepts at most six message ids per request. */
export const OCR_BATCH_LIMIT = 6;
/** Stop retrying one message after two rounds so a broken image never loops. */
export const OCR_MAX_MESSAGE_ATTEMPTS = 2;

export const OCR_EMPTY_PLACEHOLDER = "[图片，未识别]";
export const OCR_FAILED_PLACEHOLDER = "[图片 OCR 失败]";
export const OCR_TIMEOUT_PLACEHOLDER = "[图片 OCR 超时]";

/* ------------------------------------------------------------------ */
/* wire shapes (normalized, renderer-friendly)                         */
/* ------------------------------------------------------------------ */

export interface WecomRecentChat {
  conversationId: string;
  displayName: string;
  /** Local-only alias shown instead of displayName when set. */
  alias?: string;
  /** False when the conversation is no longer visible in the WeCom directory. */
  available?: boolean;
  /** Prompt library id bound to this chat ("" = first eq_reply prompt). */
  promptId?: string;
  /** Optional local role note appended to the bound prompt. */
  promptNote?: string;
  /** Seconds since epoch; 0 when the runtime did not report one. */
  lastMessageTime: number;
  /** Id of the newest message, used to pick the "new message" hint / ordering tiebreak. */
  lastMessageId?: string;
  isGroup: boolean;
}

/** One entry of the `chat.catalog` search directory. */
export interface WecomCatalogChat extends WecomRecentChat {
  /** Already present in the user's curated chat list. */
  added: boolean;
}

export interface WecomImageDescriptor {
  index: number;
  available: boolean;
  mimeType: string;
  errorCode: string;
}

export interface WecomContextMessage {
  messageId: string;
  senderId: string;
  senderName: string;
  /** Seconds since epoch. */
  sendTime: number;
  contentType: string;
  text: string;
  images: WecomImageDescriptor[];
  imageCount: number;
}

export interface WecomChatContext {
  conversationId: string;
  displayName: string;
  isGroup: boolean;
  /** True when the runtime fell back to older messages for a quiet chat. */
  windowFallback: boolean;
  /** True when older pages exist before the earliest loaded message. */
  hasMore: boolean;
  /** Account owner identity; v4.6.4 does not mark own messages in the stream. */
  selfSenderId?: string;
  selfSenderName?: string;
  messages: WecomContextMessage[];
}

/**
 * Snapshot of a message selected for the reply. Kept independent of the loaded
 * context so polls/pagination never drop or rewrite the user's selection.
 */
export interface SelectedMessageSnapshot {
  messageId: string;
  senderName: string;
  sendTime: number;
  text: string;
  imageCount: number;
}

/** Focus payload for `assistant_action("eq_reply")` (v4.6.5). */
export interface EqFocusMessage {
  senderName: string;
  timeLabel: string;
  text: string;
}

export interface ChatListPayload {
  chats: WecomRecentChat[];
  /** Runtime revision used as `expectedRevision` for curated-chat commands. */
  revision: number;
}

export interface ChatCatalogPayload {
  chats: WecomCatalogChat[];
  revision: number;
}

export type OcrSource = "ocr" | "llm";

export interface OcrRecord {
  messageId: string;
  index: number;
  text: string;
  source: OcrSource;
  model: string;
  error: string;
}

/** OCR results keyed by `messageId:index`. */
export type OcrLookup = Record<string, OcrRecord>;
/** Per-message request counters keyed by message id. */
export type OcrAttempts = Record<string, number>;

export type EqStyleId = "steady" | "warm" | "brief";

export interface EqStyle {
  id: string;
  title: string;
  content: string;
}

export interface EqSignal {
  label: string;
  detail: string;
}

export interface EqInterpretation {
  summary: string;
  signals: EqSignal[];
}

export interface EqCheck {
  risks: string[];
  suggestions: string[];
}

/** One selectable prompt from `config.prompts.items` (scene = eq_reply). */
export interface EqPromptOption {
  id: string;
  name: string;
}

/** Minimal prompt-library item shape needed to build the option list. */
export interface EqPromptItemSource {
  id: string;
  name: string;
  scene?: string;
}

export type EqFallbackReason =
  | "not_configured"
  | "timeout"
  | "vision_unsupported"
  | "http_error"
  | "network_error"
  | "invalid_response"
  | "empty"
  | "partial"
  | "unknown";

export interface EqTimings {
  imageMs: number;
  modelMs: number;
  parseMs: number;
  totalMs: number;
  attempts: number;
}

export interface EqAiMeta {
  promptId: string;
  promptFallback: boolean;
  fallbackReason?: EqFallbackReason;
  message?: string;
}

export interface EqImageMeta {
  attached: number;
  skipped?: number;
}

export interface EqReplyResult {
  styles: EqStyle[];
  interpretation: EqInterpretation;
  check: EqCheck;
  fallback: boolean;
  /** v4.6.6 generation timing breakdown. */
  timings?: EqTimings;
  /** v4.6.6 prompt resolution metadata. */
  ai?: EqAiMeta;
  /** Images actually attached / skipped by the runtime. */
  images?: EqImageMeta;
  /** Top-level fallback reason (kept alongside ai.fallbackReason for older runtimes). */
  fallbackReason?: EqFallbackReason;
}

/* ------------------------------------------------------------------ */
/* small shared helpers                                                */
/* ------------------------------------------------------------------ */

function trimText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function pad2(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/** Accepts seconds (contract) and tolerates milliseconds / numeric strings. */
export function normalizeUnixSeconds(value: unknown): number {
  const numeric = typeof value === "number"
    ? value
    : typeof value === "string" && value.trim()
      ? Number(value)
      : Number.NaN;
  if (!Number.isFinite(numeric) || numeric <= 0) return 0;
  return numeric > 1_000_000_000_000 ? Math.floor(numeric / 1000) : Math.floor(numeric);
}

function startOfLocalDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

export function chatTypeLabel(isGroup: boolean): string {
  return isGroup ? "群聊" : "私聊";
}

/** `HH:mm` for message bubbles; `--:--` when the runtime had no timestamp. */
export function formatMessageClock(sendTime: number): string {
  if (!Number.isFinite(sendTime) || sendTime <= 0) return "--:--";
  const date = new Date(sendTime * 1000);
  if (Number.isNaN(date.getTime())) return "--:--";
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

/**
 * Compact timestamp for the conversation rail: today → `HH:mm`,
 * yesterday → `昨天`, this year → `M月D日`, older → `YYYY年M月D日`.
 */
export function formatChatTime(lastMessageTime: number, now: Date = new Date()): string {
  if (!Number.isFinite(lastMessageTime) || lastMessageTime <= 0) return "";
  const date = new Date(lastMessageTime * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const today = startOfLocalDay(now);
  const target = startOfLocalDay(date);
  const dayDiff = Math.round((today - target) / 86_400_000);
  if (dayDiff === 0) return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  if (dayDiff === 1) return "昨天";
  if (date.getFullYear() === now.getFullYear()) {
    return `${date.getMonth() + 1}月${date.getDate()}日`;
  }
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
}

/* ------------------------------------------------------------------ */
/* recent chats                                                        */
/* ------------------------------------------------------------------ */

function readRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function readChatItems(raw: unknown): unknown[] {
  if (Array.isArray(raw)) return raw;
  const chats = readRecord(raw).chats;
  return Array.isArray(chats) ? chats : [];
}

function normalizeRevision(value: unknown): number {
  const numeric = typeof value === "number"
    ? value
    : typeof value === "string" && value.trim()
      ? Number(value)
      : Number.NaN;
  return Number.isFinite(numeric) && numeric >= 0 ? Math.floor(numeric) : 0;
}

/** Newest first; equal timestamps fall back to the newest message id. */
function sortChatsByActivity<T extends WecomRecentChat>(chats: readonly T[]): T[] {
  return [...chats].sort((left, right) => {
    const byTime = right.lastMessageTime - left.lastMessageTime;
    if (byTime !== 0) return byTime;
    return (right.lastMessageId ?? "").localeCompare(
      left.lastMessageId ?? "",
      undefined,
      { numeric: true },
    );
  });
}

/** Newest first; chats without a timestamp keep their relative position last. */
export function sortRecentChats(chats: readonly WecomRecentChat[]): WecomRecentChat[] {
  return sortChatsByActivity(chats);
}

function normalizeRecentChat(item: unknown): WecomRecentChat | null {
  const record = readRecord(item);
  const conversationId = trimText(record.conversationId);
  if (!conversationId) return null;
  const alias = trimText(record.alias);
  const lastMessageId = trimText(record.lastMessageId) || String(record.lastMessageId ?? "").trim();
  return {
    conversationId,
    displayName: trimText(record.displayName) || conversationId,
    ...(alias ? { alias } : {}),
    available: record.available === undefined ? true : Boolean(record.available),
    promptId: trimText(record.promptId),
    promptNote: trimText(record.promptNote),
    lastMessageTime: normalizeUnixSeconds(record.lastMessageTime),
    ...(lastMessageId ? { lastMessageId } : {}),
    isGroup: Boolean(record.isGroup),
  };
}

/** Defensive parser for the curated `chat.recent` result (only added chats). */
export function normalizeChatList(raw: unknown): ChatListPayload {
  const record = readRecord(raw);
  const revision = normalizeRevision(record.revision);
  const seen = new Set<string>();
  const chats: WecomRecentChat[] = [];
  for (const item of readChatItems(raw)) {
    const chat = normalizeRecentChat(item);
    if (!chat || seen.has(chat.conversationId)) continue;
    seen.add(chat.conversationId);
    chats.push(chat);
  }
  return { chats: sortChatsByActivity(chats), revision };
}

/** Defensive parser for the `chat.recent` query result. */
export function normalizeRecentChats(raw: unknown): WecomRecentChat[] {
  return normalizeChatList(raw).chats;
}

/** Defensive parser for the `chat.catalog` search directory. */
export function normalizeChatCatalog(raw: unknown): ChatCatalogPayload {
  const record = readRecord(raw);
  const revision = normalizeRevision(record.revision);
  const seen = new Set<string>();
  const chats: WecomCatalogChat[] = [];
  for (const item of readChatItems(raw)) {
    const chat = normalizeRecentChat(item);
    if (!chat || seen.has(chat.conversationId)) continue;
    seen.add(chat.conversationId);
    chats.push({ ...chat, added: Boolean(readRecord(item).added) });
  }
  return { chats: sortChatsByActivity(chats), revision };
}

export function defaultChatId(chats: readonly WecomRecentChat[]): string {
  return chats[0]?.conversationId ?? "";
}

/** Cheap signature used to skip re-renders when a poll returned the same list. */
export function chatsFingerprint(chats: readonly WecomRecentChat[]): string {
  return chats
    .map((chat) => [
      chat.conversationId,
      chat.displayName,
      chat.alias ?? "",
      chat.available === false ? 0 : 1,
      chat.promptId ?? "",
      chat.promptNote ?? "",
      chat.lastMessageTime,
      chat.lastMessageId ?? "",
      chat.isGroup ? 1 : 0,
    ].join("\u0001"))
    .join("\u0002");
}

/** Case-insensitive local filter over display name, alias and conversation id. */
export function filterCatalogChats(
  chats: readonly WecomCatalogChat[],
  query: string,
): WecomCatalogChat[] {
  const keyword = trimText(query).toLowerCase();
  if (!keyword) return [...chats];
  return chats.filter((chat) => (
    trimText(chat.displayName).toLowerCase().includes(keyword)
    || trimText(chat.alias).toLowerCase().includes(keyword)
    || chat.conversationId.toLowerCase().includes(keyword)
  ));
}

/** Ids in the catalog that can still be added. */
export function catalogSelectableIds(chats: readonly WecomCatalogChat[]): string[] {
  return chats
    .filter((chat) => !chat.added)
    .map((chat) => chat.conversationId);
}

/** Conversation to select after adding: the first entry that was not there before. */
export function firstAddedChatId(
  previousChats: readonly WecomRecentChat[],
  nextChats: readonly WecomRecentChat[],
): string {
  const previous = new Set(previousChats.map((chat) => chat.conversationId));
  return nextChats.find((chat) => !previous.has(chat.conversationId))?.conversationId ?? "";
}

/** Picks the next surviving chat when the current one is removed. */
export function nextChatIdAfterRemoval(
  chats: readonly WecomRecentChat[],
  removedIds: readonly string[],
  currentChatId: string,
): string {
  const removed = new Set(removedIds);
  if (!currentChatId || !removed.has(currentChatId)) return currentChatId;
  const index = chats.findIndex((chat) => chat.conversationId === currentChatId);
  if (index >= 0) {
    for (let cursor = index + 1; cursor < chats.length; cursor += 1) {
      if (!removed.has(chats[cursor].conversationId)) return chats[cursor].conversationId;
    }
    for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
      if (!removed.has(chats[cursor].conversationId)) return chats[cursor].conversationId;
    }
  }
  return "";
}

/* ------------------------------------------------------------------ */
/* per-chat prompt binding (v4.6.6)                                    */
/* ------------------------------------------------------------------ */

/**
 * Selectable prompts come from `config.prompts.items`; only `eq_reply` entries
 * (legacy items without a scene default to issue analysis) are relevant here.
 */
export function eqPromptOptions(
  items: readonly EqPromptItemSource[] | null | undefined,
): EqPromptOption[] {
  const seen = new Set<string>();
  const options: EqPromptOption[] = [];
  for (const item of items ?? []) {
    if (!item || (item.scene ?? "issue_analysis") !== "eq_reply") continue;
    const id = trimText(item.id);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    options.push({ id, name: trimText(item.name) || id });
  }
  return options.sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
}

/** True when the chat binds a prompt id that is no longer in the library. */
export function isPromptMissing(
  options: readonly EqPromptOption[],
  promptId: string,
): boolean {
  const id = trimText(promptId);
  return Boolean(id) && !options.some((item) => item.id === id);
}

/** Trims and caps the local role note (runtime validates the same limit). */
export function promptNoteValue(
  value: string,
  maxLength: number = PROMPT_NOTE_MAX_LENGTH,
): string {
  const capped = Math.max(0, Math.floor(maxLength));
  return trimText(value).slice(0, capped);
}

/* ------------------------------------------------------------------ */
/* chat context                                                        */
/* ------------------------------------------------------------------ */

function normalizeImageDescriptors(value: unknown): WecomImageDescriptor[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<number>();
  const images: WecomImageDescriptor[] = [];
  for (const item of value) {
    const record = readRecord(item);
    const indexValue = Number(record.index);
    const index = Number.isFinite(indexValue) ? Math.max(0, Math.floor(indexValue)) : images.length;
    if (seen.has(index)) continue;
    seen.add(index);
    images.push({
      index,
      available: Boolean(record.available),
      mimeType: trimText(record.mimeType),
      errorCode: trimText(record.errorCode),
    });
  }
  return images.sort((left, right) => left.index - right.index);
}

/** Defensive parser for one `chat.context` message. */
export function normalizeContextMessage(item: unknown): WecomContextMessage | null {
  const record = readRecord(item);
  const messageId = trimText(record.messageId);
  if (!messageId) return null;
  const images = normalizeImageDescriptors(record.images);
  const imageCountValue = Number(record.imageCount);
  const reportedImageCount = Number.isFinite(imageCountValue)
    ? Math.max(0, Math.floor(imageCountValue))
    : 0;
  return {
    messageId,
    senderId: trimText(record.senderId),
    senderName: trimText(record.senderName) || "未知成员",
    sendTime: normalizeUnixSeconds(record.sendTime),
    contentType: trimText(record.contentType) || "text",
    text: typeof record.text === "string" ? record.text.trim() : "",
    images,
    imageCount: Math.max(reportedImageCount, images.length),
  };
}

/** Defensive parser for the `chat.context` query result; messages run oldest → newest. */
export function normalizeContext(raw: unknown): WecomChatContext {
  const record = readRecord(raw);
  const rawMessages = Array.isArray(record.messages) ? record.messages : [];
  const messages: WecomContextMessage[] = [];
  for (const item of rawMessages) {
    const message = normalizeContextMessage(item);
    if (message) messages.push(message);
  }
  messages.sort((left, right) => left.sendTime - right.sendTime);
  const conversationId = trimText(record.conversationId);
  const selfSenderId = trimText(record.selfSenderId);
  const selfSenderName = trimText(record.selfSenderName);
  return {
    conversationId,
    displayName: trimText(record.displayName) || conversationId,
    isGroup: Boolean(record.isGroup),
    windowFallback: Boolean(record.windowFallback),
    hasMore: Boolean(record.hasMore),
    ...(selfSenderId ? { selfSenderId } : {}),
    ...(selfSenderName ? { selfSenderName } : {}),
    messages,
  };
}

/**
 * Merges one page into the loaded context: de-duplicates by message id and
 * keeps the stream sorted oldest → newest. Newer copies of a message win so a
 * refresh can update the same id; equal timestamps keep their load order.
 */
export function mergeContextMessages(
  current: readonly WecomContextMessage[],
  incoming: readonly WecomContextMessage[],
): WecomContextMessage[] {
  const byId = new Map<string, WecomContextMessage>();
  for (const message of current) byId.set(message.messageId, message);
  for (const message of incoming) byId.set(message.messageId, message);
  return [...byId.values()].sort((left, right) => left.sendTime - right.sendTime);
}

/** Signature of the rendered context; equal means a poll changed nothing. */
export function contextFingerprint(messages: readonly WecomContextMessage[]): string {
  return messages
    .map((message) => [
      message.messageId,
      message.sendTime,
      message.senderId,
      message.senderName,
      message.text,
      Math.max(message.imageCount, message.images.length),
      message.images
        .map((image) => `${image.index}:${image.available ? 1 : 0}:${image.errorCode}`)
        .join(","),
    ].join("\u0001"))
    .join("\u0002");
}

/** Keeps the user's target while it is still in the window, else uses the last message. */
export function nextReplyTargetId(
  messages: readonly WecomContextMessage[],
  currentMessageId: string,
): string {
  if (currentMessageId && messages.some((message) => message.messageId === currentMessageId)) {
    return currentMessageId;
  }
  // Skip blank trailing messages (common in bot chats) so the default target
  // is something the user can actually reply to.
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const candidate = messages[index];
    if (messageDisplayText(candidate) !== "[空消息]") return candidate.messageId;
  }
  return messages.length > 0 ? messages[messages.length - 1].messageId : "";
}

/* ------------------------------------------------------------------ */
/* message selection (v4.6.5 multi-select replies)                     */
/* ------------------------------------------------------------------ */

/** Snapshot used by the reply panel; survives context refreshes and pagination. */
export function messageSnapshot(message: WecomContextMessage): SelectedMessageSnapshot {
  return {
    messageId: message.messageId,
    senderName: senderDisplayName(message.senderName, message.senderId),
    sendTime: message.sendTime,
    text: messageBodyText(message).trim() || "[空消息]",
    imageCount: Math.max(message.imageCount, message.images.length),
  };
}

function sortSelection(
  selection: readonly SelectedMessageSnapshot[],
): SelectedMessageSnapshot[] {
  return [...selection].sort((left, right) => left.sendTime - right.sendTime);
}

export function selectionFingerprint(selection: readonly SelectedMessageSnapshot[]): string {
  return sortSelection(selection).map((item) => item.messageId).join("\u0001");
}

/** Adds the message to the selection, or removes it when already selected. */
export function toggleMessageSelection(
  selection: readonly SelectedMessageSnapshot[],
  message: WecomContextMessage,
): SelectedMessageSnapshot[] {
  if (selection.some((item) => item.messageId === message.messageId)) {
    return removeSelectedMessage(selection, message.messageId);
  }
  return sortSelection([...selection, messageSnapshot(message)]);
}

export function removeSelectedMessage(
  selection: readonly SelectedMessageSnapshot[],
  messageId: string,
): SelectedMessageSnapshot[] {
  return selection.filter((item) => item.messageId !== messageId);
}

/**
 * Default reply focus on entering a conversation: direct chats keep the
 * one-to-one habit of selecting the newest message, groups start empty so the
 * reply can be composed from the whole thread.
 */
export function defaultMessageSelection(
  messages: readonly WecomContextMessage[],
  isGroup: boolean,
): SelectedMessageSnapshot[] {
  if (isGroup || messages.length === 0) return [];
  return [messageSnapshot(messages[messages.length - 1])];
}

export function selectionRecipient(
  selection: readonly SelectedMessageSnapshot[],
): string {
  const ordered = sortSelection(selection);
  return ordered.length > 0 ? ordered[ordered.length - 1].senderName : "";
}

/** Fallback `text` for rule-based replies, ordered oldest → newest. */
export function selectionText(selection: readonly SelectedMessageSnapshot[]): string {
  return sortSelection(selection)
    .map((item) => item.text.trim())
    .filter(Boolean)
    .join("\n");
}

/** `focusMessages` payload; keeps at most the newest 20 selected messages. */
export function focusMessagesPayload(
  selection: readonly SelectedMessageSnapshot[],
  limit: number = FOCUS_MESSAGES_LIMIT,
): EqFocusMessage[] {
  const capped = Math.max(0, Math.floor(limit));
  if (capped === 0) return [];
  return sortSelection(selection)
    .slice(-capped)
    .map((item) => ({
      senderName: item.senderName,
      timeLabel: formatMessageClock(item.sendTime),
      text: item.text,
    }));
}

/**
 * Image union for the generation call: selected messages' images first (they
 * are the focus), then the newest context images, de-duplicated and capped.
 */
export function collectReplyImageIds(
  contextMessages: readonly WecomContextMessage[],
  selection: readonly SelectedMessageSnapshot[],
  limit: number = 3,
): string[] {
  const capped = Math.max(0, Math.floor(limit));
  if (capped === 0) return [];
  const ids: string[] = [];
  const selected = [...selection].sort((left, right) => right.sendTime - left.sendTime);
  for (const item of selected) {
    if (item.imageCount <= 0 || !item.messageId || ids.includes(item.messageId)) continue;
    ids.push(item.messageId);
    if (ids.length >= capped) return ids;
  }
  for (let index = contextMessages.length - 1; index >= 0; index -= 1) {
    const message = contextMessages[index];
    const imageCount = Math.max(message.imageCount, message.images.length);
    if (imageCount === 0 || !message.messageId || ids.includes(message.messageId)) continue;
    ids.push(message.messageId);
    if (ids.length >= capped) return ids;
  }
  return ids;
}

/**
 * Manual mode only needs pasted text. Otherwise a selection is enough, and a
 * conversation with loaded context can generate even with zero selected
 * messages ("combine the thread").
 */
export function canGenerateReply(input: {
  manualMode: boolean;
  manualText: string;
  selection: readonly SelectedMessageSnapshot[];
  contextMessageCount: number;
}): boolean {
  if (input.manualMode) return trimText(input.manualText).length > 0;
  return input.selection.length > 0 || input.contextMessageCount > 0;
}

/* ------------------------------------------------------------------ */
/* scroll viewport                                                     */
/* ------------------------------------------------------------------ */

export interface ContextScrollMetrics {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/** True while the user is close enough to the bottom to follow new messages. */
export function isNearBottom(
  metrics: ContextScrollMetrics,
  threshold: number = STICK_TO_BOTTOM_THRESHOLD,
): boolean {
  if (!Number.isFinite(metrics.scrollHeight) || metrics.scrollHeight <= 0) return true;
  return metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight < threshold;
}

/**
 * The previous page is requested only when the stream is scrollable and the
 * user actually reached the top; a not-yet-filled viewport must not loop pages.
 */
export function shouldLoadOlder(
  input: ContextScrollMetrics & { hasMore: boolean; loadingOlder: boolean },
  threshold: number = LOAD_OLDER_THRESHOLD,
): boolean {
  if (!input.hasMore || input.loadingOlder) return false;
  if (input.scrollTop > threshold) return false;
  return input.scrollHeight > input.clientHeight + 8;
}

/* ------------------------------------------------------------------ */
/* renderer-side context cache (stale-while-revalidate)                */
/* ------------------------------------------------------------------ */

export interface CachedChatContext {
  context: WecomChatContext;
  /** `Date.now()` when the payload was cached. */
  at: number;
}

export type ChatContextCache = Record<string, CachedChatContext>;

/** Fresh cached context for a conversation, or null when missing/expired. */
export function readCachedContext(
  cache: ChatContextCache,
  conversationId: string,
  now: number,
  ttl: number = CONTEXT_CACHE_TTL_MS,
): WecomChatContext | null {
  const entry = cache[conversationId];
  if (!entry) return null;
  if (!Number.isFinite(entry.at) || now < entry.at || now - entry.at > ttl) return null;
  return entry.context;
}

/** Stores one conversation and drops expired entries; returns a new cache object. */
export function writeCachedContext(
  cache: ChatContextCache,
  conversationId: string,
  context: WecomChatContext,
  now: number,
  ttl: number = CONTEXT_CACHE_TTL_MS,
): ChatContextCache {
  const next: ChatContextCache = {};
  for (const [key, entry] of Object.entries(cache)) {
    if (Number.isFinite(entry.at) && now >= entry.at && now - entry.at <= ttl) {
      next[key] = entry;
    }
  }
  if (conversationId) next[conversationId] = { context, at: now };
  return next;
}

/* ------------------------------------------------------------------ */
/* OCR                                                                 */
/* ------------------------------------------------------------------ */

export function ocrLookupKey(messageId: string, index: number): string {
  return `${messageId}:${index}`;
}

function normalizeOcrSource(value: unknown): OcrSource {
  return trimText(value) === "llm" ? "llm" : "ocr";
}

/** Defensive parser for the `chat.ocr` query result. */
export function normalizeOcrResults(raw: unknown): OcrRecord[] {
  const record = readRecord(raw);
  const results = Array.isArray(raw) ? raw : Array.isArray(record.results) ? record.results : [];
  const normalized: OcrRecord[] = [];
  for (const item of results) {
    const entry = readRecord(item);
    const messageId = trimText(entry.messageId);
    const indexValue = Number(entry.index);
    if (!messageId || !Number.isFinite(indexValue) || indexValue < 0) continue;
    normalized.push({
      messageId,
      index: Math.floor(indexValue),
      text: trimText(entry.text),
      source: normalizeOcrSource(entry.source),
      model: trimText(entry.model),
      error: trimText(entry.error),
    });
  }
  return normalized;
}

/** Merges one `chat.ocr` response into the accumulated lookup (never drops old rows). */
export function mergeOcrRecords(current: OcrLookup, raw: unknown): OcrLookup {
  const merged: OcrLookup = { ...current };
  for (const record of normalizeOcrResults(raw)) {
    merged[ocrLookupKey(record.messageId, record.index)] = record;
  }
  return merged;
}

/** True when the runtime reported that no OCR/vision engine is configured. */
export function ocrEngineAvailable(raw: unknown): boolean {
  const engine = readRecord(readRecord(raw).engine);
  return Boolean(engine.available);
}

/**
 * Display copy for one image: the recognized text, or a placeholder. The
 * backend already uses human-readable Chinese errors, so this only classifies
 * them into `[图片，未识别]` / `[图片 OCR 失败]` / `[图片 OCR 超时]`.
 */
export function imageOcrText(record: OcrRecord | undefined): string {
  if (!record) return OCR_EMPTY_PLACEHOLDER;
  const text = trimText(record.text);
  if (text) return text;
  const error = trimText(record.error);
  if (!error) return OCR_EMPTY_PLACEHOLDER;
  if (/超时|超预算|timeout|timed out/i.test(error)) return OCR_TIMEOUT_PLACEHOLDER;
  if (/未配置|图片不可用|超过单轮|not configured|no .{0,12}(?:ocr|vision|model)|unavailable/i.test(error)) {
    return OCR_EMPTY_PLACEHOLDER;
  }
  return OCR_FAILED_PLACEHOLDER;
}

export function ocrSourceLabel(record: OcrRecord | undefined): string {
  return record?.source === "llm" ? "全局视觉模型识别" : "图片 OCR 识别";
}

/** One message as plain text: original text plus per-image OCR results. */
export function messageBodyText(
  message: WecomContextMessage,
  lookup: OcrLookup = {},
): string {
  const imageCount = Math.max(message.imageCount, message.images.length);
  const parts: string[] = [];
  const display = messageDisplayText(message);
  if (display) parts.push(display);
  if (imageCount > 0) {
    let hasOcr = false;
    for (let index = 0; index < imageCount; index += 1) {
      const record = lookup[ocrLookupKey(message.messageId, index)];
      if (!record?.text.trim()) continue;
      parts.push(imageOcrText(record));
      hasOcr = true;
    }
    if (!hasOcr) parts.push("[图片]");
  }
  const body = parts.join("\n").trim();
  return body || "[空消息]";
}

/**
 * Composes the model context as `[HH:mm] 发送者: 内容` lines (oldest first) and
 * inlines OCR text for image messages. Selected messages can be excluded so the
 * model does not receive the same content twice (target row + context).
 */
export interface ComposedContext {
  text: string;
  /** True when the composed text exceeded the character budget and was cut. */
  truncated: boolean;
}

/**
 * Composes the model context as `[HH:mm] sender: content` lines (oldest first)
 * and inlines OCR text for image messages. Selected messages can be excluded so
 * the model does not receive the same content twice, and the result is capped
 * at `CONTEXT_TEXT_MAX_CHARS` (the runtime truncates as well; the flag drives
 * the "上下文已截断" hint in the UI).
 */
export function composeContext(
  messages: readonly WecomContextMessage[],
  lookup: OcrLookup = {},
  options: {
    excludeMessageId?: string;
    excludeMessageIds?: readonly string[];
    maxChars?: number;
  } = {},
): ComposedContext {
  const excluded = new Set<string>(options.excludeMessageIds ?? []);
  if (options.excludeMessageId) excluded.add(options.excludeMessageId);
  const full = messages
    .filter((message) => !excluded.has(message.messageId))
    .map((message) => {
      const body = messageBodyText(message, lookup)
        .replace(/\s*\r?\n\s*/g, " ")
        .replace(/[ \t]+/g, " ")
        .trim();
      return `[${formatMessageClock(message.sendTime)}] ${message.senderName}: ${body}`;
    })
    .join("\n");
  const maxChars = Math.max(0, Math.floor(options.maxChars ?? CONTEXT_TEXT_MAX_CHARS));
  if (maxChars === 0 || full.length <= maxChars) {
    return { text: full, truncated: false };
  }
  return { text: full.slice(0, maxChars).trimEnd(), truncated: true };
}

/** String-only variant kept for call sites that do not need the truncation bit. */
export function composeContextText(
  messages: readonly WecomContextMessage[],
  lookup: OcrLookup = {},
  options: { excludeMessageId?: string; excludeMessageIds?: readonly string[] } = {},
): string {
  return composeContext(messages, lookup, options).text;
}

export function isMessageOcrResolved(message: WecomContextMessage, lookup: OcrLookup): boolean {
  const imageCount = Math.max(message.imageCount, message.images.length);
  if (imageCount === 0) return true;
  for (let index = 0; index < imageCount; index += 1) {
    if (!lookup[ocrLookupKey(message.messageId, index)]) return false;
  }
  return true;
}

/**
 * Message ids for the next `chat.ocr` round: image messages that still lack a
 * result and have not exhausted their retries. Batches stay within the
 * backend's six-message limit, and the caller loops the batches serially.
 */
export function nextOcrBatch(
  messages: readonly WecomContextMessage[],
  lookup: OcrLookup,
  attempts: OcrAttempts = {},
  limit: number = OCR_BATCH_LIMIT,
): string[] {
  const batch: string[] = [];
  const cappedLimit = Math.max(1, Math.floor(limit));
  for (const message of messages) {
    if (batch.length >= cappedLimit) break;
    if (Math.max(message.imageCount, message.images.length) === 0) continue;
    if ((attempts[message.messageId] ?? 0) >= OCR_MAX_MESSAGE_ATTEMPTS) continue;
    if (isMessageOcrResolved(message, lookup)) continue;
    if (!batch.includes(message.messageId)) batch.push(message.messageId);
  }
  return batch;
}

/* ------------------------------------------------------------------ */
/* polling                                                             */
/* ------------------------------------------------------------------ */

/** Polling only runs while App keeps the page active and the window is visible. */
export function shouldPoll(
  active: boolean | undefined,
  visibilityState: string | undefined,
): boolean {
  return active !== false && visibilityState === "visible";
}

/* ------------------------------------------------------------------ */
/* eq_reply                                                            */
/* ------------------------------------------------------------------ */

export const EQ_STYLE_ORDER: readonly EqStyleId[] = ["steady", "warm", "brief"];

export const EQ_STYLE_LABELS: Record<string, string> = {
  steady: "稳重",
  warm: "亲和",
  brief: "简短",
};

export function eqStyleLabel(id: string): string {
  return EQ_STYLE_LABELS[id] ?? id;
}

/** Orders the three styles as 稳重/亲和/简短; unknown ids keep their order at the end. */
export function sortEqStyles<T extends { id: string }>(styles: readonly T[]): T[] {
  const order = EQ_STYLE_ORDER as readonly string[];
  return [...styles].sort((left, right) => {
    const leftIndex = order.indexOf(left.id);
    const rightIndex = order.indexOf(right.id);
    return (leftIndex < 0 ? 99 : leftIndex) - (rightIndex < 0 ? 99 : rightIndex);
  });
}

function stringList(value: unknown): string[] {
  if (typeof value === "string") {
    return value
      .split(/\r?\n/)
      .map((line) => line.replace(/^[-*·\d.、)\s]+/, "").trim())
      .filter(Boolean);
  }
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (typeof item === "string") return item.trim();
      if (item && typeof item === "object") {
        const record = readRecord(item);
        return trimText(record.text) || trimText(record.detail) || trimText(record.title);
      }
      return "";
    })
    .filter(Boolean);
}

function normalizeEqStyle(raw: unknown, fallbackId: EqStyleId): EqStyle | null {
  const record = readRecord(raw);
  const content = trimText(record.content);
  if (!content) return null;
  const id = trimText(record.id) || fallbackId;
  const title = trimText(record.title) || EQ_STYLE_LABELS[id] || id;
  return { id, title, content };
}

function normalizeEqSignals(value: unknown): EqSignal[] {
  if (!Array.isArray(value)) return [];
  const signals: EqSignal[] = [];
  for (const item of value) {
    const record = readRecord(item);
    const label = trimText(record.label);
    const detail = trimText(record.detail);
    if (!label && !detail) continue;
    signals.push({ label: label || "信号", detail });
  }
  return signals;
}

function normalizeEqInterpretation(value: unknown): EqInterpretation {
  const record = readRecord(value);
  return {
    summary: trimText(record.summary),
    signals: normalizeEqSignals(record.signals),
  };
}

function normalizeEqCheck(value: unknown): EqCheck {
  const record = readRecord(value);
  return {
    risks: stringList(record.risks),
    suggestions: stringList(record.suggestions),
  };
}

const EQ_FALLBACK_REASONS: readonly EqFallbackReason[] = [
  "not_configured",
  "timeout",
  "vision_unsupported",
  "http_error",
  "network_error",
  "invalid_response",
  "empty",
  "partial",
  "unknown",
];

/** Unknown or missing reasons degrade to `unknown` / undefined for display. */
export function normalizeFallbackReason(value: unknown): EqFallbackReason | undefined {
  const reason = trimText(value);
  if (!reason) return undefined;
  return (EQ_FALLBACK_REASONS as readonly string[]).includes(reason)
    ? reason as EqFallbackReason
    : "unknown";
}

function nonNegativeInteger(value: unknown): number | undefined {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric >= 0 ? Math.round(numeric) : undefined;
}

function normalizeEqTimings(value: unknown): EqTimings | undefined {
  const record = readRecord(value);
  if (Object.keys(record).length === 0) return undefined;
  const imageMs = nonNegativeInteger(record.imageMs) ?? 0;
  const modelMs = nonNegativeInteger(record.modelMs) ?? 0;
  const parseMs = nonNegativeInteger(record.parseMs) ?? 0;
  const totalMs = nonNegativeInteger(record.totalMs) ?? imageMs + modelMs + parseMs;
  const attempts = Math.max(1, nonNegativeInteger(record.attempts) ?? 1);
  return { imageMs, modelMs, parseMs, totalMs, attempts };
}

function normalizeEqAi(value: unknown): EqAiMeta | undefined {
  const record = readRecord(value);
  if (Object.keys(record).length === 0) return undefined;
  const fallbackReason = normalizeFallbackReason(record.fallbackReason);
  const message = trimText(record.message);
  return {
    promptId: trimText(record.promptId),
    promptFallback: Boolean(record.promptFallback),
    ...(fallbackReason ? { fallbackReason } : {}),
    ...(message ? { message } : {}),
  };
}

function normalizeEqImages(value: unknown): EqImageMeta | undefined {
  const record = readRecord(value);
  if (Object.keys(record).length === 0) return undefined;
  const attached = nonNegativeInteger(record.attached) ?? 0;
  const skipped = nonNegativeInteger(record.skipped);
  return { attached, ...(skipped !== undefined ? { skipped } : {}) };
}

/** Duration copy for the result header: 840 ms → "840 毫秒", 8421 ms → "8.4 秒". */
export function formatDurationMs(value: unknown): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return "";
  if (numeric < 1_000) return `${Math.round(numeric)} 毫秒`;
  const seconds = numeric / 1_000;
  if (seconds < 10) return `${seconds.toFixed(1).replace(/\.0$/, "")} 秒`;
  return `${Math.round(seconds)} 秒`;
}

/**
 * Human explanation for a local-template fallback. The old one-size-fits-all
 * fallback copy is intentionally gone; each reason explains what happened and
 * what the user can do next.
 */
export function fallbackReasonCopy(reason: EqFallbackReason | string | undefined): string {
  switch (normalizeFallbackReason(reason)) {
    case "not_configured":
      return "还没有配置可用的大模型，当前先用本地模板生成草稿。到「设置 → 模型与 OCR」配置后，才能得到 AI 版本。";
    case "timeout":
      return "这次 AI 响应超时，已先用本地模板生成草稿。可以稍后重新生成；如果经常超时，建议减少图片或缩短上下文。";
    case "vision_unsupported":
      return "当前模型不支持图片识别，本次按文字内容生成。可以换支持图片的模型，或直接修改草稿。";
    case "partial":
      return "AI 返回的内容不完整，缺失部分已用本地模板补齐，建议核对后再发送。";
    case "network_error":
      return "连接模型服务时中断，已先用本地模板生成草稿；可以稍后重新生成。";
    case "invalid_response":
      return "AI 返回格式异常（可能被 token 上限截断），已先用本地模板生成草稿；可以稍后重新生成。";
    case "empty":
      return "AI 返回为空（可能输出被截断），已先用本地模板生成草稿；可以稍后重新生成或减少上下文。";
    case "http_error":
      return "模型服务返回错误，已先用本地模板生成草稿；请检查模型地址、密钥和模型名称后重试。";
    default:
      return "这次没有拿到 AI 结果，已先用本地模板生成草稿；可直接修改后复制。";
  }
}

/** True when the fallback points the user at the model settings page. */
export function fallbackReasonNeedsSettings(reason: EqFallbackReason | string | undefined): boolean {
  return normalizeFallbackReason(reason) === "not_configured";
}

/** Image ids sent with a generation request; the toggle disables them entirely. */
export function imageIdsForPayload(
  enabled: boolean,
  imageIds: readonly string[],
  limit: number = 3,
): string[] {
  if (!enabled) return [];
  const capped = Math.max(0, Math.floor(limit));
  if (capped === 0) return [];
  const ids: string[] = [];
  for (const value of imageIds) {
    const id = trimText(value);
    if (!id || ids.includes(id)) continue;
    ids.push(id);
    if (ids.length >= capped) break;
  }
  return ids;
}

/** Defensive parser for the `eq_reply` assistant action response. */
export function normalizeEqResult(raw: unknown): EqReplyResult {
  const record = readRecord(raw);
  const seen = new Set<string>();
  const styles: EqStyle[] = [];
  if (Array.isArray(record.styles)) {
    for (const item of record.styles) {
      const style = normalizeEqStyle(item, "steady");
      if (!style || seen.has(style.id)) continue;
      seen.add(style.id);
      styles.push(style);
    }
  }
  const timings = normalizeEqTimings(record.timings);
  const ai = normalizeEqAi(record.ai);
  const images = normalizeEqImages(record.images);
  const fallbackReason =
    normalizeFallbackReason(record.fallbackReason) ?? ai?.fallbackReason;
  return {
    styles: sortEqStyles(styles),
    interpretation: normalizeEqInterpretation(record.interpretation),
    check: normalizeEqCheck(record.check),
    fallback: Boolean(record.fallback),
    ...(timings ? { timings } : {}),
    ...(ai ? { ai } : {}),
    ...(images ? { images } : {}),
    ...(fallbackReason ? { fallbackReason } : {}),
  };
}

/** Initial editable drafts for the three style cards. */
export function replyDrafts(result: EqReplyResult | null): Record<string, string> {
  const drafts: Record<string, string> = {};
  for (const style of result?.styles ?? []) drafts[style.id] = style.content;
  return drafts;
}

/** Payload for `bridge.assistantAction("eq_reply", …)`; empty extras are omitted. */
export function eqReplyPayload(input: {
  text: string;
  context?: string;
  recipient?: string;
  conversationId?: string;
  promptId?: string;
  promptNote?: string;
  imageMessageIds?: readonly string[];
  focusMessages?: readonly EqFocusMessage[];
}): Record<string, unknown> {
  const payload: Record<string, unknown> = { text: trimText(input.text) };
  const context = trimText(input.context);
  if (context) payload.context = context;
  const recipient = trimText(input.recipient);
  if (recipient) payload.recipient = recipient;
  const conversationId = trimText(input.conversationId);
  if (conversationId) payload.conversationId = conversationId;
  const promptId = trimText(input.promptId);
  if (promptId) payload.promptId = promptId;
  const promptNote = promptNoteValue(input.promptNote ?? "");
  if (promptNote) payload.promptNote = promptNote;
  const imageIds = (input.imageMessageIds ?? [])
    .map((value) => trimText(value))
    .filter(Boolean)
    .slice(0, 3);
  if (imageIds.length > 0) payload.imageMessageIds = imageIds;
  const focusMessages = (input.focusMessages ?? [])
    .map((item) => ({
      senderName: trimText(item.senderName),
      timeLabel: trimText(item.timeLabel),
      text: trimText(item.text),
    }))
    .filter((item) => item.text)
    .slice(0, FOCUS_MESSAGES_LIMIT);
  if (focusMessages.length > 0) payload.focusMessages = focusMessages;
  return payload;
}

/** Newest image messages (max six) that should travel with the generation call. */
export function collectContextImageIds(
  messages: readonly WecomContextMessage[],
  limit: number = 6,
): string[] {
  const ids: string[] = [];
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (Math.max(message.imageCount, message.images.length) === 0) continue;
    if (!message.messageId || ids.includes(message.messageId)) continue;
    ids.push(message.messageId);
    if (ids.length >= limit) break;
  }
  return ids;
}

/* ------------------------------------------------------------------ */
/* display hardening                                                   */
/* ------------------------------------------------------------------ */

const CONTROL_CHAR_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;
const ZERO_WIDTH_RE = /[\u200b-\u200f\u202a-\u202e\u2060\ufeff]/g;
const APP_PAYLOAD_HINTS = [
  '"source_icon_desc"',
  '"display"',
  '"main_title"',
  '"selected"',
  '"target":',
  '"icon_url"',
];
const PRIORITY_APP_KEYS = [
  "title",
  "main_title",
  "sub_title",
  "subtitle",
  "content",
  "text",
  "desc",
  "description",
  "summary",
  "display",
  "name",
  "label",
];
const TECHNICAL_VALUE_RE = /^[A-Za-z0-9_.:/\\-]{1,80}$/;
const MAX_MESSAGE_TEXT_CHARS = 2_000;
const MAX_APP_SUMMARY_CHARS = 400;
const CONTENT_TYPE_LABELS: Record<string, string> = {
  app: "应用消息",
  link: "链接",
  file: "文件",
  voice: "语音",
  image: "图片",
};
const NUMERIC_SENDER_RE = /^\d{6,}$/;

/**
 * Human label for a conversation. Direct chats often arrive with only their
 * raw `S:<corp>_<userId>` id as a name; showing that id looked like a bug.
 */
export function chatDisplayName(
  chat: Pick<WecomRecentChat, "conversationId" | "displayName" | "isGroup"> & { alias?: string },
): string {
  const alias = trimText(chat.alias);
  if (alias) return alias;
  const name = trimText(chat.displayName);
  const looksLikeId =
    !name ||
    name === chat.conversationId ||
    /^[SR]:[0-9A-Za-z_:.-]+$/.test(name);
  if (!looksLikeId) return name;
  return chat.isGroup ? "群聊" : "私聊会话";
}

/** Sender fallback when WeCom has no user_table name for the sender id. */
export function senderDisplayName(senderName: string, senderId = ""): string {
  const name = trimText(senderName);
  if (name && !NUMERIC_SENDER_RE.test(name) && name !== senderId) return name;
  return "未知成员";
}

export function contentTypeLabel(contentType: string): string {
  const value = trimText(contentType);
  if (value.startsWith("unsupported:")) return "非文本消息";
  return CONTENT_TYPE_LABELS[value] ?? "非文本消息";
}

/** Make raw chat text safe to display: app cards become readable labels. */
export function sanitizeMessageText(value: unknown): string {
  let text = typeof value === "string" ? value : "";
  text = text.replace(ZERO_WIDTH_RE, "").replace(CONTROL_CHAR_RE, "");
  text = text.replace(/\n{3,}/g, "\n\n").trim();
  if (!text) return "";
  if (looksLikeAppPayload(text)) {
    const summary = summarizeAppMessage(text);
    if (summary) return summary;
  }
  if (text.length <= MAX_MESSAGE_TEXT_CHARS) return text;
  return `${text.slice(0, MAX_MESSAGE_TEXT_CHARS)}…`;
}

export function messageDisplayText(message: WecomContextMessage): string {
  const imageCount = Math.max(message.imageCount, message.images.length);
  // Image messages store download URLs / metadata in their text field; the
  // image chip and OCR block carry the useful content, so never echo the URL.
  if (imageCount > 0) return "";
  const text = sanitizeMessageText(message.text);
  // Whitespace / punctuation-only payloads used to render as blank bubbles.
  if (text && !containsReadableText(text)) return fallbackMessageLabel(message);
  if (text) return text;
  return fallbackMessageLabel(message);
}

function fallbackMessageLabel(message: WecomContextMessage): string {
  if (!message.contentType || message.contentType === "text") return "[空消息]";
  return `[${contentTypeLabel(message.contentType)}]`;
}

function containsReadableText(text: string): boolean {
  return /[\p{L}\p{N}]/u.test(text);
}

/**
 * Only genuinely long chat text is folded (more than 600 characters or 12
 * lines); normal messages stay fully readable without an extra click.
 */
export function shouldCollapseMessage(
  text: string,
  maxChars: number = COLLAPSE_MESSAGE_MAX_CHARS,
  maxLines: number = COLLAPSE_MESSAGE_MAX_LINES,
): boolean {
  if (!text) return false;
  if (text.length > maxChars) return true;
  return text.split("\n").length > maxLines;
}

function looksLikeAppPayload(text: string): boolean {
  const value = text.replace(/^\ufeff/, "").trimStart();
  if (!value.startsWith("{") && !value.startsWith("[")) return false;
  if (value.length < 24) return false;
  if (APP_PAYLOAD_HINTS.some((hint) => value.includes(hint))) return true;
  return /"(?:title|content|desc|display|summary)"\s*:/.test(value);
}

function summarizeAppMessage(text: string): string {
  const raw = text.trim();
  const values: string[] = [];
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    values.push(...quotedJsonValues(raw));
  }
  if (payload !== undefined) collectAppStrings(payload, values, 0);

  const readable: string[] = [];
  let total = 0;
  for (const value of values) {
    const candidate = String(value ?? "").trim();
    if (!candidate || readable.includes(candidate)) continue;
    if (isTechnicalAppValue(candidate)) continue;
    readable.push(candidate);
    total += candidate.length;
    if (readable.length >= 5 || total >= 300) break;
  }
  if (readable.length === 0) return "[应用消息]";
  return readable.join(" · ").slice(0, MAX_APP_SUMMARY_CHARS);
}

function collectAppStrings(value: unknown, out: string[], depth: number): void {
  if (depth > 4 || out.length >= 24) return;
  if (typeof value === "string") {
    out.push(value);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value.slice(0, 8)) collectAppStrings(item, out, depth + 1);
    return;
  }
  if (!value || typeof value !== "object") return;
  const record = value as Record<string, unknown>;
  for (const key of PRIORITY_APP_KEYS) {
    if (key in record) collectAppStrings(record[key], out, depth + 1);
  }
  for (const [key, item] of Object.entries(record)) {
    if (PRIORITY_APP_KEYS.includes(key)) continue;
    collectAppStrings(item, out, depth + 1);
  }
}

function quotedJsonValues(text: string): string[] {
  const values: string[] = [];
  for (const match of text.matchAll(/"([A-Za-z0-9_-]+)"\s*:\s*"((?:[^"\\]|\\.){0,200})"/g)) {
    values.push(match[2]);
  }
  for (const match of text.matchAll(/"([^"\\]{2,200})"/g)) values.push(match[1]);
  return values;
}

function isTechnicalAppValue(value: string): boolean {
  const lower = value.toLowerCase();
  if (value.length <= 1 || ["0", "1", "true", "false", "null", "yes", "no"].includes(lower)) {
    return true;
  }
  if (
    value.startsWith("http://") ||
    value.startsWith("https://") ||
    value.startsWith("data:") ||
    value.startsWith("icon_") ||
    value.startsWith("source_")
  ) {
    return true;
  }
  if (/^[0-9a-fA-F_-]{16,}$/.test(value)) return true;
  if (TECHNICAL_VALUE_RE.test(value) && !/[\u4e00-\u9fff]/.test(value)) return true;
  return false;
}

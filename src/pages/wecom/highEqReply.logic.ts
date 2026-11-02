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
/** Newest 30 messages… */
export const CONTEXT_LIMIT = 30;
/** …within the last six hours. */
export const CONTEXT_WINDOW_SECONDS = 6 * 60 * 60;
export const RECENT_CHAT_LIMIT = 40;
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
  /** Seconds since epoch; 0 when the runtime did not report one. */
  lastMessageTime: number;
  isGroup: boolean;
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
  messages: WecomContextMessage[];
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

export interface EqReplyResult {
  styles: EqStyle[];
  interpretation: EqInterpretation;
  check: EqCheck;
  fallback: boolean;
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

/** Newest first; chats without a timestamp keep their relative position last. */
export function sortRecentChats(chats: readonly WecomRecentChat[]): WecomRecentChat[] {
  return [...chats].sort((left, right) => right.lastMessageTime - left.lastMessageTime);
}

/** Defensive parser for the `chat.recent` query result. */
export function normalizeRecentChats(raw: unknown): WecomRecentChat[] {
  const seen = new Set<string>();
  const chats: WecomRecentChat[] = [];
  for (const item of readChatItems(raw)) {
    const record = readRecord(item);
    const conversationId = trimText(record.conversationId);
    if (!conversationId || seen.has(conversationId)) continue;
    seen.add(conversationId);
    chats.push({
      conversationId,
      displayName: trimText(record.displayName) || conversationId,
      lastMessageTime: normalizeUnixSeconds(record.lastMessageTime),
      isGroup: Boolean(record.isGroup),
    });
  }
  return sortRecentChats(chats);
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
      chat.lastMessageTime,
      chat.isGroup ? 1 : 0,
    ].join("\u0001"))
    .join("\u0002");
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
  return {
    conversationId,
    displayName: trimText(record.displayName) || conversationId,
    isGroup: Boolean(record.isGroup),
    messages,
  };
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
  return messages.length > 0 ? messages[messages.length - 1].messageId : "";
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
  const text = trimText(message.text);
  // Image messages often carry a plain "[图片]" placeholder; the OCR block
  // already explains the image, so do not repeat it.
  if (text && !(imageCount > 0 && /^\[图[片像]\]$/.test(text))) parts.push(text);
  for (let index = 0; index < imageCount; index += 1) {
    parts.push(imageOcrText(lookup[ocrLookupKey(message.messageId, index)]));
  }
  const body = parts.join("\n").trim();
  if (body) return body;
  return message.contentType && message.contentType !== "text" ? `[${message.contentType}]` : "[空消息]";
}

/**
 * Composes the model context as `[HH:mm] 发送者: 内容` lines (oldest first) and
 * inlines OCR text for image messages. The reply target can be excluded so the
 * model does not receive the same message twice.
 */
export function composeContextText(
  messages: readonly WecomContextMessage[],
  lookup: OcrLookup = {},
  options: { excludeMessageId?: string } = {},
): string {
  return messages
    .filter((message) => !options.excludeMessageId || message.messageId !== options.excludeMessageId)
    .map((message) => {
      const body = messageBodyText(message, lookup)
        .replace(/\s*\r?\n\s*/g, " ")
        .replace(/[ \t]+/g, " ")
        .trim();
      return `[${formatMessageClock(message.sendTime)}] ${message.senderName}: ${body}`;
    })
    .join("\n");
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
  return {
    styles: sortEqStyles(styles),
    interpretation: normalizeEqInterpretation(record.interpretation),
    check: normalizeEqCheck(record.check),
    fallback: Boolean(record.fallback),
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
}): Record<string, unknown> {
  const payload: Record<string, unknown> = { text: trimText(input.text) };
  const context = trimText(input.context);
  if (context) payload.context = context;
  const recipient = trimText(input.recipient);
  if (recipient) payload.recipient = recipient;
  return payload;
}

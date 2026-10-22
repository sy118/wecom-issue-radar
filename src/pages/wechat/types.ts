/** Local UI types for the personal WeChat workbench (M2). */

export interface WechatAccount {
  accountId: string;
  dbDir: string;
  root?: string;
  selected: boolean;
  score?: number;
  reason?: string;
}

export interface WechatDetectCandidate {
  /** Absolute `db_storage` path. */
  path: string;
  accountId: string;
  root: string;
  reason: string;
  score: number;
  valid: boolean;
  hasSession: boolean;
  shardCount: number;
  modifiedAt: number;
  source: string;
  selected: boolean;
}

export interface WechatDetectResult {
  found: boolean;
  dbDir: string;
  accounts: WechatAccount[];
  candidates: WechatDetectCandidate[];
  running: boolean;
  version: string;
  reason: string;
  checkedPaths: string[];
  scannedAt: string;
  /** True when the configured directory was reused without a full scan. */
  cached?: boolean;
  forced?: boolean;
  platformSupported?: boolean;
  selected?: boolean;
  ready?: boolean;
  keyCount?: number;
  databaseCount?: number;
  coveredDatabaseCount?: number;
  imageKeyConfigured?: boolean;
  imageKeyVerified?: boolean;
}

export interface WechatScanResult {
  ok: boolean;
  message: string;
  accountId: string;
}

export interface WechatImageKeyResult {
  ok: boolean;
  message: string;
  code?: string;
  retryable?: boolean;
  imageKeyConfigured?: boolean;
  imageKeyVerified?: boolean;
}

export interface ReplyStyle {
  id: string;
  label: string;
  instruction: string;
}

export const DEFAULT_REPLY_STYLES: ReplyStyle[] = [
  { id: "shorter", label: "更简短", instruction: "保持原意，改得更短、更直接，适合微信发送。" },
  { id: "politer", label: "更礼貌", instruction: "保持事实不变，语气更礼貌、体贴，但不要过度客套。" },
  { id: "casual", label: "更随意", instruction: "保持事实不变，改成更自然随意的微信口吻。" },
  { id: "another", label: "换一个", instruction: "重新拟一份自然、有帮助且不过度承诺的回复。" },
];

export interface WechatImportResult {
  imported: boolean;
  message: string;
  source?: string;
  counts?: Record<string, number>;
}

export type ChatKind = "direct" | "group";

export interface WechatChat {
  chatId: string;
  accountId: string;
  displayName: string;
  kind: ChatKind;
  lastMessage: string;
  /** Seconds since epoch (tolerant of milliseconds). */
  lastTimestamp: number;
  unread: number;
}

export type TriggerMode = "auto" | "manual" | "rules";

export interface WechatTriggerPolicy {
  mode: TriggerMode;
  keywords: string[];
  mentionMe: boolean;
  quietWindowMs: number;
}

export interface WechatProject {
  id: string;
  accountId: string;
  chatId: string;
  chatTitle: string;
  name: string;
  kind: ChatKind;
  enabled: boolean;
  summary: string;
  tone: string;
  prompt: string;
  trigger: WechatTriggerPolicy;
  providerId: string;
  skillPaths: string[];
  mcpServerIds: string[];
  revision: number;
  createdAt: string;
  updatedAt: string;
  /** Unknown backend fields are preserved when the project is saved again. */
  [key: string]: unknown;
}

export type MessageDirection = "incoming" | "outgoing" | "system";

export type MessageKind =
  | "text"
  | "image"
  | "voice"
  | "video"
  | "link"
  | "file"
  | "quote"
  | "system"
  | "unknown";

export interface WechatQuote {
  sender: string;
  text: string;
}

export interface WechatMessage {
  id: string;
  projectId: string;
  sourceDb: string;
  localId: number;
  sortSeq: number;
  /** Seconds since epoch (tolerant of milliseconds). */
  createTime: number;
  senderId: string;
  senderName: string;
  direction: MessageDirection;
  kind: MessageKind;
  text: string;
  quote: WechatQuote | null;
  media: Array<Record<string, unknown>>;
  mentionedMe: boolean;
}

export type DraftStatus = "idle" | "generating" | "ready" | "error" | "stale";

export interface WechatDraft {
  projectId: string;
  content: string;
  status: DraftStatus;
  stale: boolean;
  model: string;
  errorMessage: string;
  updatedAt: string;
  basedOn: string[];
}

export interface WechatProvider {
  id: string;
  name: string;
  kind: string;
  baseUrl: string;
  model: string;
  hasSecret: boolean;
  [key: string]: unknown;
}

export interface WechatMcpServer {
  id: string;
  name: string;
  transport: string;
  enabled: boolean;
  toolCount: number;
  lastError: string;
  [key: string]: unknown;
}

export interface WechatSettings {
  dbDir: string;
  pollIntervalSeconds: number;
  contextMessageLimit: number;
  contextCharacterLimit: number;
  /** Editable rewrite presets used by the draft panel. */
  replyStyles?: ReplyStyle[];
  /** Default tone applied to newly created projects. */
  defaultTone?: string;
  /** Default trigger mode for new projects ("" = follow chat kind). */
  defaultTriggerMode?: string;
  [key: string]: unknown;
}

export interface WechatMediaPreview {
  dataUrl: string;
  mime: string;
}

export interface WechatExportResult {
  dir: string;
  files: string[];
}

export interface WechatPollProject {
  projectId: string;
  newMessages: WechatMessage[];
  draft: WechatDraft | null;
  unreadDelta: number;
}

export interface WechatConfigSnapshot {
  providers: WechatProvider[];
  mcpServers: WechatMcpServer[];
}

export const DEFAULT_CONTEXT_MESSAGE_LIMIT = 50;
export const MESSAGE_PAGE_STEP = 50;
export const MAX_MESSAGE_LIMIT = 500;
export const POLL_INTERVAL_MS = 10_000;

export const TONE_PRESETS = [
  "自然友好",
  "专业稳重",
  "简洁直接",
  "温和耐心",
  "轻松随意",
];

export const emptyDraft = (projectId: string): WechatDraft => ({
  projectId,
  content: "",
  status: "idle",
  stale: false,
  model: "",
  errorMessage: "",
  updatedAt: "",
  basedOn: [],
});

export const defaultTrigger = (kind: ChatKind): WechatTriggerPolicy => ({
  mode: kind === "group" ? "manual" : "auto",
  keywords: [],
  mentionMe: false,
  quietWindowMs: 1800,
});

export interface WechatProjectDefaults {
  tone?: string;
  triggerMode?: TriggerMode;
}

export function newWechatProject(
  chat: WechatChat,
  providers: WechatProvider[],
  defaults: WechatProjectDefaults = {},
): WechatProject {
  const trigger = defaultTrigger(chat.kind);
  const defaultMode = defaults.triggerMode;
  if (defaultMode && (defaultMode !== "rules" || chat.kind === "group")) {
    trigger.mode = defaultMode;
  }
  return {
    id: typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `wechat_project_${Date.now()}`,
    accountId: chat.accountId,
    chatId: chat.chatId,
    chatTitle: chat.displayName,
    name: chat.displayName,
    kind: chat.kind,
    enabled: true,
    summary: "",
    tone: defaults.tone || "自然友好",
    prompt: "",
    trigger,
    providerId: providers[0]?.id ?? "",
    skillPaths: [],
    mcpServerIds: [],
    revision: 0,
    createdAt: "",
    updatedAt: "",
  };
}

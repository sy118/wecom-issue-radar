/** Local UI types for the personal WeChat workbench (M2). */

export interface WechatAccount {
  accountId: string;
  dbDir: string;
  selected: boolean;
}

export interface WechatDetectResult {
  found: boolean;
  dbDir: string;
  accounts: WechatAccount[];
  running: boolean;
  version: string;
}

export interface WechatScanResult {
  ok: boolean;
  message: string;
  accountId: string;
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

export function newWechatProject(
  chat: WechatChat,
  providers: WechatProvider[],
): WechatProject {
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
    tone: "自然友好",
    prompt: "",
    trigger: defaultTrigger(chat.kind),
    providerId: providers[0]?.id ?? "",
    skillPaths: [],
    mcpServerIds: [],
    revision: 0,
    createdAt: "",
    updatedAt: "",
  };
}

/**
 * Pure helpers and tolerant normalizers for the personal WeChat workbench.
 *
 * The Python sidecar may return camelCase or snake_case payloads depending on
 * the action version, so every value that crosses the bridge is normalized here
 * and unit-tested without React or Electron.
 */

import {
  DEFAULT_CONTEXT_MESSAGE_LIMIT,
  DEFAULT_REPLY_STYLES,
  MESSAGE_PAGE_STEP,
  MAX_MESSAGE_LIMIT,
  emptyDraft,
  type ChatKind,
  type DraftStatus,
  type MessageDirection,
  type MessageKind,
  type ReplyStyle,
  type TriggerMode,
  type WechatChat,
  type WechatConfigSnapshot,
  type WechatDetectResult,
  type WechatDraft,
  type WechatExportResult,
  type WechatMcpServer,
  type WechatMediaPreview,
  type WechatMessage,
  type WechatPollProject,
  type WechatProject,
  type WechatProvider,
  type WechatScanResult,
  type WechatSettings,
  type WechatTriggerPolicy,
} from "./types";

// ---------------------------------------------------------------------------
// Small tolerant readers.
// ---------------------------------------------------------------------------

export function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function pickValue(record: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key];
  }
  return undefined;
}

function pickString(
  record: Record<string, unknown>,
  keys: string[],
  fallback = "",
): string {
  const value = pickValue(record, keys);
  return typeof value === "string" ? value : fallback;
}

function pickNumber(
  record: Record<string, unknown>,
  keys: string[],
  fallback = 0,
): number {
  const value = pickValue(record, keys);
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return fallback;
}

function pickBool(
  record: Record<string, unknown>,
  keys: string[],
  fallback = false,
): boolean {
  const value = pickValue(record, keys);
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return value === "true" || value === "1";
  return fallback;
}

function pickArray(record: Record<string, unknown>, keys: string[]): unknown[] {
  const value = pickValue(record, keys);
  return Array.isArray(value) ? value : [];
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value
        .map((item) => (typeof item === "string" ? item.trim() : ""))
        .filter(Boolean)
    : [];
}

function unwrap(raw: unknown, keys: string[]): unknown {
  const record = asRecord(raw);
  if (!record) return raw;
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key];
  }
  return raw;
}

/** Converts seconds/milliseconds/ISO strings into seconds since epoch. */
export function normalizeTimestamp(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 10_000_000_000 ? Math.floor(value / 1000) : Math.floor(value);
  }
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return normalizeTimestamp(numeric);
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return Math.floor(parsed / 1000);
  }
  return 0;
}

// ---------------------------------------------------------------------------
// Normalizers.
// ---------------------------------------------------------------------------

export function normalizeDetect(raw: unknown): WechatDetectResult {
  const value = unwrap(raw, ["wechat", "status", "detect"]) ?? {};
  const record = asRecord(value) ?? {};
  const accountsRaw = pickArray(record, ["accounts", "profiles"]);
  const accounts = accountsRaw
    .map((item) => {
      if (typeof item === "string") {
        return { accountId: item, dbDir: "", selected: false };
      }
      const account = asRecord(item);
      if (!account) return null;
      return {
        accountId: pickString(account, ["accountId", "account_id", "id", "wxid"]),
        dbDir: pickString(account, ["dbDir", "db_dir", "path"]),
        selected: pickBool(account, ["selected", "active"]),
      };
    })
    .filter((item): item is NonNullable<typeof item> => item !== null);
  const found = pickBool(
    record,
    ["found", "detected", "configured"],
    accounts.length > 0
      || Boolean(
        pickString(record, [
          "dbDir",
          "db_dir",
          "configuredDbDir",
          "configured_db_dir",
        ]),
      ),
  );
  const candidatesRaw = pickArray(record, ["candidates"]);
  const candidates = candidatesRaw
    .map((item) => {
      const candidate = asRecord(item);
      if (!candidate) return null;
      const path = pickString(candidate, ["path", "dbDir", "db_dir"]);
      if (!path) return null;
      return {
        path,
        accountId: pickString(candidate, ["accountId", "account_id", "id"]),
        root: pickString(candidate, ["root", "accountRoot", "account_root"]),
        reason: pickString(candidate, ["reason", "detail", "message"]),
        score: pickNumber(candidate, ["score", "priority"], 0),
        valid: pickBool(candidate, ["valid", "usable", "readable"], false),
        hasSession: pickBool(candidate, ["hasSession", "has_session"], false),
        shardCount: pickNumber(candidate, ["shardCount", "shard_count"], 0),
        modifiedAt: pickNumber(candidate, ["modifiedAt", "modified_at"], 0),
        source: pickString(candidate, ["source", "label"]),
        selected: pickBool(candidate, ["selected", "active"]),
      };
    })
    .filter((item): item is NonNullable<typeof item> => item !== null);
  return {
    found,
    dbDir: pickString(record, ["dbDir", "db_dir", "configuredDbDir", "configured_db_dir"]),
    accounts,
    candidates,
    running: pickBool(record, ["running", "wechatRunning", "wechat_running"]),
    version: pickString(record, ["version", "wechatVersion", "wechat_version"]),
    reason: pickString(record, ["reason", "message", "error"]),
    checkedPaths: pickArray(record, ["checkedPaths", "checked_paths", "checked"]).filter(
      (item): item is string => typeof item === "string",
    ),
    scannedAt: pickString(record, ["scannedAt", "scanned_at"]),
    platformSupported: pickBool(record, ["platformSupported", "platform_supported"], true),
    selected: pickBool(record, ["selected"]),
    ready: pickBool(record, ["ready"]),
    keyCount: pickNumber(record, ["keyCount", "key_count"], 0),
    databaseCount: pickNumber(record, ["databaseCount", "database_count"], 0),
    coveredDatabaseCount: pickNumber(record, ["coveredDatabaseCount", "covered_database_count"], 0),
    imageKeyConfigured: pickBool(record, ["imageKeyConfigured", "image_key_configured"]),
    imageKeyVerified: pickBool(record, ["imageKeyVerified", "image_key_verified"]),
    cached: pickBool(record, ["cached"]),
    forced: pickBool(record, ["forced"]),
  };
}

export function normalizeScanResult(raw: unknown): WechatScanResult {
  const value = unwrap(raw, ["scan", "result"]) ?? {};
  const record = asRecord(value) ?? {};
  return {
    ok: pickBool(record, ["ok", "success", "scanned"], false),
    message: pickString(record, ["message", "detail", "reason", "error"], ""),
    accountId: pickString(record, ["accountId", "account_id"]),
  };
}

export function normalizeChat(raw: unknown): WechatChat | null {
  const record = asRecord(raw);
  if (!record) return null;
  const chatId = pickString(record, ["chatId", "chat_id", "conversationId", "conversation_id", "username"]);
  const displayName =
    pickString(record, ["displayName", "display_name", "name", "remark", "nickname"]) ||
    chatId ||
    "未命名会话";
  if (!chatId) return null;
  const kindText = pickString(record, ["kind", "chatKind", "chat_kind", "type"]).toLowerCase();
  const kind: ChatKind =
    kindText.includes("group") || chatId.includes("@chatroom") ? "group" : "direct";
  return {
    chatId,
    accountId: pickString(record, ["accountId", "account_id"]),
    displayName,
    kind,
    lastMessage: pickString(record, ["lastMessage", "last_message", "preview", "digest"]),
    lastTimestamp: normalizeTimestamp(
      pickValue(record, ["lastTimestamp", "last_timestamp", "lastTime", "last_time"]),
    ),
    unread: Math.max(0, pickNumber(record, ["unread", "unreadCount", "unread_count"], 0)),
  };
}

export function normalizeChats(raw: unknown): WechatChat[] {
  const list = unwrap(raw, ["chats", "items", "conversations"]);
  const values = Array.isArray(list) ? list : [];
  return values
    .map(normalizeChat)
    .filter((chat): chat is WechatChat => chat !== null);
}

function normalizeTriggerMode(value: unknown, kind: ChatKind): TriggerMode {
  const text = typeof value === "string" ? value.toLowerCase() : "";
  if (text === "auto" || text === "automatic") return "auto";
  if (text === "rules" || text === "rule") return "rules";
  if (text === "manual") return "manual";
  return kind === "group" ? "manual" : "auto";
}

export function normalizeTriggerPolicy(raw: unknown, kind: ChatKind): WechatTriggerPolicy {
  const record = asRecord(raw) ?? {};
  return {
    mode: normalizeTriggerMode(pickValue(record, ["mode", "triggerMode", "trigger_mode"]), kind),
    keywords: asStringArray(pickValue(record, ["keywords", "keyword"])),
    mentionMe: pickBool(record, ["mentionMe", "mention_me", "atMe", "at_me"]),
    quietWindowMs: Math.max(
      0,
      pickNumber(record, ["quietWindowMs", "quiet_window_ms"], 1800),
    ),
  };
}

export function normalizeProject(raw: unknown): WechatProject | null {
  const base = unwrap(raw, ["project", "saved", "item"]);
  const record = asRecord(base);
  if (!record) return null;
  const id = pickString(record, ["id", "projectId", "project_id"]);
  if (!id) return null;
  const chatId = pickString(record, ["chatId", "chat_id", "conversationId", "conversation_id"]);
  const kindText = pickString(record, ["kind", "chatKind", "chat_kind"]).toLowerCase();
  const kind: ChatKind =
    kindText.includes("group") || chatId.includes("@chatroom") ? "group" : "direct";
  const chatTitle =
    pickString(record, ["chatTitle", "chat_title", "displayName", "display_name"]) ||
    pickString(record, ["name"]);
  return {
    ...record,
    id,
    accountId: pickString(record, ["accountId", "account_id"]),
    chatId,
    chatTitle,
    name: pickString(record, ["name", "title"]) || chatTitle || "未命名项目",
    kind,
    enabled: pickBool(record, ["enabled", "active"], true),
    summary: pickString(record, ["summary", "projectSummary", "project_summary"]),
    tone: pickString(record, ["tone", "tonePreset", "tone_preset"]) || "自然友好",
    prompt: pickString(record, ["prompt", "systemPrompt", "system_prompt"]),
    trigger: normalizeTriggerPolicy(
      pickValue(record, ["trigger", "triggerPolicy", "trigger_policy"]),
      kind,
    ),
    providerId: pickString(record, [
      "providerId",
      "provider_id",
      "modelProviderId",
      "model_provider_id",
    ]),
    skillPaths: asStringArray(pickValue(record, ["skillPaths", "skill_paths"])),
    mcpServerIds: asStringArray(pickValue(record, ["mcpServerIds", "mcp_server_ids"])),
    revision: pickNumber(record, ["revision"], 0),
    createdAt: pickString(record, ["createdAt", "created_at"]),
    updatedAt: pickString(record, ["updatedAt", "updated_at"]),
  };
}

export function normalizeProjects(raw: unknown): WechatProject[] {
  const list = unwrap(raw, ["projects", "items"]);
  const values = Array.isArray(list) ? list : [];
  return values
    .map(normalizeProject)
    .filter((project): project is WechatProject => project !== null);
}

function normalizeDirection(value: unknown): MessageDirection {
  const text = typeof value === "string" ? value.toLowerCase() : "";
  if (text === "outgoing" || text === "out" || text === "self" || text === "me") {
    return "outgoing";
  }
  if (text === "system") return "system";
  return "incoming";
}

const MESSAGE_KINDS: MessageKind[] = [
  "text",
  "image",
  "voice",
  "video",
  "link",
  "file",
  "quote",
  "system",
  "unknown",
];

function normalizeMessageKind(value: unknown): MessageKind {
  const text = typeof value === "string" ? value.toLowerCase() : "";
  return (MESSAGE_KINDS.find((kind) => text.includes(kind)) ?? "text") as MessageKind;
}

function normalizeQuote(raw: unknown): WechatQuoteLike | null {
  const record = asRecord(raw);
  if (!record) return null;
  const text = pickString(record, ["text", "content", "body"]);
  const sender = pickString(record, ["sender", "senderName", "sender_name", "name"]);
  if (!text && !sender) return null;
  return { sender, text };
}

type WechatQuoteLike = { sender: string; text: string };

export function normalizeMessages(raw: unknown, projectId = ""): WechatMessage[] {
  const list = unwrap(raw, ["messages", "items", "newMessages", "new_messages"]);
  const values = Array.isArray(list) ? list : [];
  const messages: WechatMessage[] = [];
  values.forEach((item, index) => {
    const record = asRecord(item);
    if (!record) return;
    const localId = pickNumber(record, ["localId", "local_id"]);
    const sortSeq = pickNumber(record, ["sortSeq", "sort_seq"]);
    const sourceDb = pickString(record, ["sourceDb", "source_db"]);
    const createTime = normalizeTimestamp(
      pickValue(record, ["createTime", "create_time", "timestamp", "ts", "time"]),
    );
    const messageProjectId =
      pickString(record, ["projectId", "project_id"]) || projectId;
    const id =
      pickString(record, ["id", "messageId", "message_id"]) ||
      `${messageProjectId}:${sourceDb}:${localId}:${sortSeq}:${createTime}:${index}`;
    messages.push({
      id,
      projectId: messageProjectId,
      sourceDb,
      localId,
      sortSeq,
      createTime,
      senderId: pickString(record, ["senderId", "sender_id", "sender"]),
      senderName: pickString(record, ["senderName", "sender_name", "sender"]),
      direction: normalizeDirection(pickValue(record, ["direction", "dir"])),
      kind: normalizeMessageKind(pickValue(record, ["kind", "type", "messageType", "message_type"])),
      text: pickString(record, ["text", "content", "body"]),
      quote: normalizeQuote(pickValue(record, ["quote", "quoted", "reply"])),
      media: pickArray(record, ["media", "mediaList", "media_list"]).filter(
        (item): item is Record<string, unknown> => asRecord(item) !== null,
      ),
      mentionedMe: pickBool(record, ["mentionedMe", "mentioned_me", "atMe", "at_me"]),
    });
  });
  return mergeMessages([], messages);
}

export function normalizeDraft(raw: unknown): WechatDraft | null {
  const value = unwrap(raw, ["draft", "currentDraft", "current_draft"]);
  const record = asRecord(value);
  if (!record) return null;
  const statusText = pickString(record, ["status"]).toLowerCase();
  const content = pickString(record, ["content", "text"]);
  const status: DraftStatus =
    statusText === "generating" || statusText === "ready" || statusText === "error" || statusText === "stale"
      ? (statusText as DraftStatus)
      : content
        ? "ready"
        : "idle";
  return {
    projectId: pickString(record, ["projectId", "project_id"]),
    content,
    status,
    stale: pickBool(record, ["stale", "expired"]) || status === "stale",
    model: pickString(record, ["model", "modelName", "model_name"]),
    errorMessage: pickString(record, ["errorMessage", "error_message", "error", "reason"]),
    updatedAt: pickString(record, ["updatedAt", "updated_at"]),
    basedOn: asStringArray(pickValue(record, ["basedOn", "based_on"])),
  };
}

export function normalizeProviders(raw: unknown): WechatConfigSnapshot {
  const root = asRecord(raw) ?? {};
  const providerList = unwrap(raw, ["providers", "items"]);
  const providersRaw = Array.isArray(providerList) ? providerList : [];
  const providers: WechatProvider[] = providersRaw
    .map((item) => {
      const record = asRecord(item);
      if (!record) return null;
      const id = pickString(record, ["id", "providerId", "provider_id"]);
      if (!id) return null;
      return {
        ...record,
        id,
        name: pickString(record, ["name", "title"]) || id,
        kind: pickString(record, ["kind", "protocol", "type"]),
        baseUrl: pickString(record, ["baseUrl", "base_url", "url"]),
        model: pickString(record, ["model", "modelName", "model_name"]),
        hasSecret: pickBool(record, ["hasSecret", "has_secret", "secretConfigured", "secret_configured"]),
      };
    })
    .filter((provider): provider is WechatProvider => provider !== null);
  const mcpList = pickArray(root, ["mcpServers", "mcp_servers", "servers"]);
  const mcpServers: WechatMcpServer[] = mcpList
    .map((item) => {
      const record = asRecord(item);
      if (!record) return null;
      const id = pickString(record, ["id", "serverId", "server_id"]);
      if (!id) return null;
      const catalog = pickArray(record, ["catalog", "tools"]);
      return {
        ...record,
        id,
        name: pickString(record, ["name", "title"]) || id,
        transport: pickString(record, ["transport", "kind"]),
        enabled: pickBool(record, ["enabled", "active"], true),
        toolCount: pickNumber(record, ["toolCount", "tool_count"], catalog.length),
        lastError: pickString(record, ["lastError", "last_error", "error"]),
      };
    })
    .filter((server): server is WechatMcpServer => server !== null);
  return { providers, mcpServers };
}

export function normalizeSettings(raw: unknown): WechatSettings {
  const root = asRecord(raw) ?? {};
  const value = unwrap(raw, ["settings", "preferences"]);
  const record = asRecord(value) ?? {};
  return {
    ...record,
    dbDir: pickString(record, ["dbDir", "db_dir", "wechatDbDir", "wechat_db_dir"]),
    pollIntervalSeconds: Math.max(
      1,
      pickNumber(record, ["pollIntervalSeconds", "poll_interval_seconds"], 5),
    ),
    contextMessageLimit: Math.max(
      10,
      pickNumber(
        record,
        ["contextMessageLimit", "context_message_limit"],
        DEFAULT_CONTEXT_MESSAGE_LIMIT,
      ),
    ),
    contextCharacterLimit: Math.max(
      1000,
      pickNumber(
        record,
        ["contextCharacterLimit", "context_character_limit"],
        24_000,
      ),
    ),
    replyStyles: normalizeReplyStyles(record.replyStyles ?? record.reply_styles),
    defaultTone: pickString(record, ["defaultTone", "default_tone"], "自然友好"),
    defaultTriggerMode: pickString(record, ["defaultTriggerMode", "default_trigger_mode"]),
    vaultMode: pickString(root, ["vaultMode", "vault_mode"]),
    dataRoot: pickString(root, ["dataRoot", "data_root"]),
    database: pickString(root, ["database", "dbFile", "db_file"]),
    warnings: pickArray(root, ["warnings"]).filter((item): item is string => typeof item === "string"),
  };
}

export function normalizeReplyStyles(raw: unknown): ReplyStyle[] {
  if (!Array.isArray(raw)) return DEFAULT_REPLY_STYLES.map((style) => ({ ...style }));
  const styles: ReplyStyle[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    const record = asRecord(item);
    if (!record) continue;
    const id = pickString(record, ["id", "styleId", "style_id"]);
    const label = pickString(record, ["label", "name", "title"]);
    const instruction = pickString(record, ["instruction", "prompt", "text"]);
    if (!id || !label || !instruction || seen.has(id)) continue;
    seen.add(id);
    styles.push({ id, label, instruction });
  }
  return styles.length ? styles : DEFAULT_REPLY_STYLES.map((style) => ({ ...style }));
}

export function normalizeMediaPreview(raw: unknown): WechatMediaPreview {
  const value = unwrap(raw, ["preview", "media", "image"]) ?? {};
  const record = asRecord(value) ?? {};
  const dataUrl = pickString(record, ["dataUrl", "data_url"]);
  let mime = pickString(record, ["mime", "mimeType", "mime_type"]);
  if (!dataUrl) {
    throw new Error(
      pickString(record, ["reason", "error", "message"]) || "图片不可用",
    );
  }
  if (!mime) {
    const match = /^data:([^;,]+)[;,]/.exec(dataUrl);
    mime = match ? match[1] : "image/png";
  }
  return { dataUrl, mime };
}

export function normalizeExportResult(raw: unknown): WechatExportResult {
  const value = unwrap(raw, ["result", "export"]) ?? {};
  const record = asRecord(value) ?? {};
  const dir = pickString(record, ["dir", "path", "directory", "outputDir", "output_dir"]);
  const filesRaw = pickArray(record, ["files", "outputs"]);
  const files = filesRaw
    .map((item) => {
      if (typeof item === "string") return item;
      const file = asRecord(item);
      if (!file) return "";
      return pickString(file, ["path", "file", "name"]);
    })
    .filter(Boolean);
  return { dir, files };
}

export function normalizePoll(raw: unknown): WechatPollProject[] {
  const value = unwrap(raw, ["poll", "result"]) ?? raw;
  const list = Array.isArray(value) ? value : pickArray(asRecord(value) ?? {}, ["projects", "items"]);
  const entries: WechatPollProject[] = [];
  for (const item of list) {
    const record = asRecord(item);
    if (!record) continue;
    const nestedProject = asRecord(pickValue(record, ["project"]));
    const projectId =
      pickString(record, ["projectId", "project_id", "id"])
      || (nestedProject
        ? pickString(nestedProject, ["id", "projectId", "project_id"])
        : "");
    if (!projectId) continue;
    const messagesField = pickValue(record, ["newMessages", "new_messages", "messages"]);
    const newMessages = Array.isArray(messagesField)
      ? normalizeMessages(messagesField, projectId)
      : [];
    const unreadField = pickValue(record, [
      "newMessageCount",
      "new_message_count",
      "unreadDelta",
      "unread_delta",
    ]);
    const unreadDelta =
      typeof unreadField === "number" && Number.isFinite(unreadField)
        ? Math.max(0, Math.floor(unreadField))
        : newMessages.filter((message) => message.direction !== "outgoing").length;
    entries.push({
      projectId,
      newMessages,
      draft: normalizeDraft(pickValue(record, ["draft", "currentDraft", "current_draft"])),
      unreadDelta,
    });
  }
  return entries;
}

// ---------------------------------------------------------------------------
// Presentation and merge helpers.
// ---------------------------------------------------------------------------

export function messageDate(createTime: number): Date | null {
  if (!createTime) return null;
  const date = new Date(createTime * 1000);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatClock(createTime: number): string {
  const date = messageDate(createTime);
  if (!date) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function formatDayLabel(createTime: number): string {
  const date = messageDate(createTime);
  if (!date) return "日期未知";
  return date.toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "short",
  });
}

export function formatRelative(createTime: number, now = Date.now()): string {
  if (!createTime) return "";
  const delta = Math.floor(now / 1000) - createTime;
  if (delta < 60) return "刚刚";
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86_400) return `${Math.floor(delta / 3600)} 小时前`;
  const date = messageDate(createTime);
  if (!date) return "";
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

export function formatFileSizeLabel(path: string): string {
  const extension = /\.([a-z0-9]+)$/i.exec(path)?.[1];
  return extension ? extension.toUpperCase() : "FILE";
}

export function mergeMessages(
  existing: WechatMessage[],
  incoming: WechatMessage[],
): WechatMessage[] {
  const merged = new Map<string, WechatMessage>();
  for (const message of existing) merged.set(message.id, message);
  for (const message of incoming) merged.set(message.id, message);
  return [...merged.values()].sort(compareMessages);
}

function compareMessages(left: WechatMessage, right: WechatMessage): number {
  if (left.createTime !== right.createTime) return left.createTime - right.createTime;
  if (left.sortSeq !== right.sortSeq) return left.sortSeq - right.sortSeq;
  return left.id.localeCompare(right.id);
}

export interface MessageMergeResult {
  messages: WechatMessage[];
  added: number;
}

export function mergeProjectMessages(
  current: Record<string, WechatMessage[]>,
  projectId: string,
  incoming: WechatMessage[],
): { map: Record<string, WechatMessage[]>; added: number } {
  const before = current[projectId] ?? [];
  const merged = mergeMessages(before, incoming);
  return {
    map: { ...current, [projectId]: merged },
    added: merged.length - before.length,
  };
}

export function incrementUnread(
  current: Record<string, number>,
  additions: Record<string, number>,
): Record<string, number> {
  const next = { ...current };
  for (const [projectId, amount] of Object.entries(additions)) {
    if (amount > 0) next[projectId] = (next[projectId] ?? 0) + amount;
  }
  return next;
}

export function nextMessageLimit(current: number): number {
  return Math.min(MAX_MESSAGE_LIMIT, Math.max(MESSAGE_PAGE_STEP, current) + MESSAGE_PAGE_STEP);
}

export function canLoadMore(messages: WechatMessage[], limit: number): boolean {
  return messages.length >= limit && limit < MAX_MESSAGE_LIMIT;
}

/** A draft is stale when the backend flags it or newer messages arrived after it. */
export function draftIsStale(draft: WechatDraft | undefined, messages: WechatMessage[]): boolean {
  if (!draft || !draft.content.trim()) return false;
  if (draft.stale || draft.status === "stale") return true;
  const updatedAt = draft.updatedAt ? Date.parse(draft.updatedAt) : Number.NaN;
  if (!Number.isFinite(updatedAt)) return false;
  const latest = messages.at(-1);
  if (!latest || !latest.createTime) return false;
  return latest.createTime * 1000 > updatedAt;
}

export function unreadForProject(
  project: WechatProject,
  chats: WechatChat[],
  pollUnread: Record<string, number>,
): number {
  const base = chats.find((chat) => chat.chatId === project.chatId)?.unread ?? 0;
  return Math.max(base, pollUnread[project.id] ?? 0);
}

export function selectedProject(
  projects: WechatProject[],
  selectedId: string,
): WechatProject | undefined {
  return projects.find((project) => project.id === selectedId) ?? projects[0];
}

export function providerLabel(providers: WechatProvider[], providerId: string): string {
  const provider = providers.find((item) => item.id === providerId);
  if (!provider) return "未选择模型";
  return provider.model ? `${provider.name} · ${provider.model}` : provider.name;
}

export function stepStatus(
  detect: WechatDetectResult | null,
  scan: WechatScanResult | null,
): { detected: boolean; scanned: boolean } {
  return {
    detected: Boolean(detect?.found),
    scanned: Boolean(scan?.ok),
  };
}

export function draftStatusCopy(draft: WechatDraft | undefined, stale: boolean): string {
  if (!draft || (!draft.content && draft.status === "idle")) return "尚无草稿";
  if (draft.status === "generating") return "生成中";
  if (stale) return "上下文已更新";
  if (draft.status === "error") return "生成失败";
  if (draft.status === "ready") return "可采纳";
  return "已保存";
}

export { emptyDraft };

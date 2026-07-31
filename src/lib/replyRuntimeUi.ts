import type {
  McpLastTestSummary,
  McpServerSummary,
  McpToolSummary,
  ReplyDeliveryMode,
  ReplyRuntimeCommand,
  ReplyRuntimeCommandBody,
  ReplyRuntimeQuery,
  ReplyTuning,
  SecretEdit,
  ListenerToolGrant,
} from "../types";
import { errorHasCode } from "./errors";

export interface McpCatalogHealth {
  error?: unknown;
}

export function mcpCatalogSnapshotCopy(updatedAt: string | undefined, toolCount: number): string {
  const normalizedToolCount = Number.isFinite(toolCount)
    ? Math.max(0, Math.floor(toolCount))
    : 0;
  const timestamp = updatedAt?.trim() ?? "";
  if (!timestamp) return `最后成功目录：尚无成功记录 · ${normalizedToolCount} 个工具`;
  const date = new Date(timestamp);
  const renderedTimestamp = Number.isNaN(date.getTime())
    ? timestamp
    : date.toLocaleString("zh-CN", { hour12: false });
  return `最后成功目录：${renderedTimestamp} · ${normalizedToolCount} 个工具`;
}

export function mcpLastTestCopy(input: {
  enabled: boolean;
  cachedCatalogAvailable: boolean;
  lastTest?: McpLastTestSummary;
}): { label: string; tone: "neutral" | "success" | "warning" | "danger" | "progress"; description: string } {
  if (!input.enabled) return {
    label: "已停用",
    tone: "neutral",
    description: "服务配置已停用。",
  };
  if (input.lastTest?.status === "success") return {
    label: "最近测试成功",
    tone: "success",
    description: "最近一次连接测试成功。",
  };
  if (input.lastTest?.status === "failed") {
    const error = typeof input.lastTest.error === "string"
      ? input.lastTest.error.trim()
      : input.lastTest.error && typeof input.lastTest.error === "object"
        ? String((input.lastTest.error as Record<string, unknown>).message ?? "").trim()
        : "";
    const reason = error || "未返回具体原因";
    return input.cachedCatalogAvailable ? {
      label: "最近测试失败",
      tone: "warning",
      description: `最近一次连接测试失败：${reason}。已保留上次成功发现的工具，不影响现有授权。`,
    } : {
      label: "最近测试失败",
      tone: "danger",
      description: `最近一次连接测试失败：${reason}。当前没有可用的工具目录。`,
    };
  }
  return {
    label: "尚未测试",
    tone: "neutral",
    description: "尚未执行连接测试。",
  };
}

export type McpServerGrantHealth = Pick<McpServerSummary, "enabled"> & {
  catalog?: McpCatalogHealth;
};

export function mcpCatalogAllowsGrants(
  server: McpServerGrantHealth | undefined,
  catalog: McpCatalogHealth | undefined,
): boolean {
  return Boolean(server?.enabled) && !server?.catalog?.error && !catalog?.error;
}

export function mcpCatalogUnavailableLabel(
  server: McpServerGrantHealth | undefined,
  catalog: McpCatalogHealth | undefined,
): string {
  if (!server?.enabled) return "服务已停用 · 不可授权";
  if (server.catalog?.error || catalog?.error) return "目录已过期 · 不可授权";
  return "";
}

export function isMcpToolGrantable(
  tool: Pick<McpToolSummary, "schemaStatus" | "schemaSha256">,
): boolean {
  return tool.schemaStatus === "current" && Boolean(tool.schemaSha256);
}

export function runtimeAvailabilityCopy(running: boolean | undefined): {
  label: string;
  description: string;
} {
  if (running === true) {
    return {
      label: "运行模块在线",
      description: "应用最小化时继续监听；退出后不会补处理离线消息。",
    };
  }
  if (running === false) {
    return {
      label: "运行模块已停止",
      description: "后台运行模块当前没有监听群消息。",
    };
  }
  return {
    label: "运行状态未知",
    description: "暂时无法读取后台运行状态，请刷新后重试。",
  };
}

export interface ListenerDraftState {
  id?: string;
  revision?: number;
  name: string;
  enabled: boolean;
  groupId: string;
  groupName: string;
  systemPrompt: string;
  webhookUrl: string;
  webhookConfigured: boolean;
  webhookVerified: boolean;
  deliveryMode: ReplyDeliveryMode;
  selectedTools: string[];
  tuning: ReplyTuning;
}

export const defaultReplyTuning = (): ReplyTuning => ({
  pollIntervalSeconds: 5,
  sameSenderMergeSeconds: 20,
  humanReplyWaitSeconds: 120,
  sessionTimeoutSeconds: 1800,
  maxConcurrency: 4,
  mcpTimeoutSeconds: 900,
});

export const defaultListenerDraft = (): ListenerDraftState => ({
  name: "",
  enabled: false,
  groupId: "",
  groupName: "",
  systemPrompt: "只回答能够通过已授权 MCP 工具检索并获得可靠证据的问题。证据不足时不要猜测。",
  webhookUrl: "",
  webhookConfigured: false,
  webhookVerified: false,
  deliveryMode: "review",
  selectedTools: [],
  tuning: defaultReplyTuning(),
});

export async function executeListenerSave<Result>(input: {
  draft: Pick<ListenerDraftState, "id" | "revision">;
  body: ReplyRuntimeCommandBody;
  readRevision: () => Promise<number>;
  execute: (command: ReplyRuntimeCommand) => Promise<Result>;
}): Promise<Result> {
  const existingRevision = input.draft.id ? input.draft.revision : undefined;
  if (input.draft.id && existingRevision === undefined) {
    throw new Error("监听器配置版本缺失，请刷新后重新编辑。");
  }
  const initialRevision = existingRevision ?? await input.readRevision();
  try {
    return await input.execute(createCommand(input.body, initialRevision));
  } catch (error) {
    if (input.draft.id || !errorHasCode(error, "REVISION_CONFLICT")) throw error;
    const refreshedRevision = await input.readRevision();
    return input.execute(createCommand(input.body, refreshedRevision));
  }
}

export function automaticDeliveryBlockers(input: {
  deliveryMode: ReplyDeliveryMode;
  webhookConfigured: boolean;
  webhookVerified: boolean;
  selectedToolCount: number;
}): string[] {
  if (input.deliveryMode !== "automatic") return [];
  const blockers: string[] = [];
  if (!input.webhookConfigured) blockers.push("请先配置群机器人的 webhook。 ".trim());
  else if (!input.webhookVerified) blockers.push("请先发送测试消息，并确认它出现在当前选择的群聊中。");
  if (input.selectedToolCount === 0) blockers.push("请至少授权一个已经发现的 MCP 工具。");
  return blockers;
}

export const secretEditForExistingValue = (): SecretEdit => ({ mode: "keep" });

export const secretEditForNewValue = (value: string | Record<string, string>): SecretEdit => ({
  mode: "replace",
  value,
});

export function parseRecordSecretEdit(
  mode: "keep" | "replace" | "clear",
  text: string,
): SecretEdit {
  if (mode === "keep") return { mode: "keep" };
  if (mode === "clear") return { mode: "clear" };
  const parsed = JSON.parse(text || "{}") as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)
    || Object.values(parsed).some((value) => typeof value !== "string")) {
    throw new Error("秘密配置必须是键值均为字符串的 JSON 对象。");
  }
  return { mode: "replace", value: parsed as Record<string, string> };
}

export function toolGrantSelectionKey(serverId: string, toolName: string, schemaSha256: string): string {
  return `grant:${encodeURIComponent(serverId)}:${encodeURIComponent(toolName)}:${schemaSha256}`;
}

export function toolGrantFromSelectionKey(key: string): Pick<ListenerToolGrant, "serverId" | "toolName" | "schemaSha256"> | null {
  const parts = key.startsWith("grant:") ? key.slice(6).split(":") : key.split(":");
  if (parts.length < 3) return null;
  const schemaSha256 = parts.pop() ?? "";
  const serverId = parts.shift() ?? "";
  const toolName = parts.join(":");
  try {
    return {
      serverId: key.startsWith("grant:") ? decodeURIComponent(serverId) : serverId,
      toolName: key.startsWith("grant:") ? decodeURIComponent(toolName) : toolName,
      schemaSha256,
    };
  } catch {
    return null;
  }
}

export function toggleToolSelection(selected: string[], key: string): string[] {
  if (selected.includes(key)) return selected.filter((item) => item !== key);
  const nextGrant = toolGrantFromSelectionKey(key);
  if (!nextGrant) return [...selected, key];
  return [
    ...selected.filter((item) => {
      const grant = toolGrantFromSelectionKey(item);
      return !grant || grant.serverId !== nextGrant.serverId || grant.toolName !== nextGrant.toolName;
    }),
    key,
  ];
}

export type McpServerGrantState = "none" | "partial" | "all";

export function mcpServerGrantState(
  selectedKeys: string[],
  serverToolKeys: string[],
): McpServerGrantState {
  const tools = new Set(serverToolKeys);
  if (tools.size === 0) return "none";
  const selected = new Set(selectedKeys);
  let selectedToolCount = 0;
  for (const key of tools) if (selected.has(key)) selectedToolCount += 1;
  if (selectedToolCount === 0) return "none";
  return selectedToolCount === tools.size ? "all" : "partial";
}

export function toggleMcpServerGrant(
  selectedKeys: string[],
  serverToolKeys: string[],
): string[] {
  const selected = [...new Set(selectedKeys)];
  const tools = [...new Set(serverToolKeys)];
  if (tools.length === 0) return selected;
  const toolSet = new Set(tools);
  if (mcpServerGrantState(selected, tools) === "all") {
    return selected.filter((key) => !toolSet.has(key));
  }
  const selectedSet = new Set(selected);
  return [...selected, ...tools.filter((key) => !selectedSet.has(key))];
}

export function tuningValidationErrors(tuning: ReplyTuning): string[] {
  const errors: string[] = [];
  const valid = (value: number, min: number, max: number) => Number.isInteger(value) && value >= min && value <= max;
  if (!valid(tuning.pollIntervalSeconds, 2, 60)) errors.push("监听刷新间隔必须在 2–60 秒之间。");
  if (!valid(tuning.sameSenderMergeSeconds, 2, 120)) errors.push("连续补充合并间隔必须在 2–120 秒之间。");
  if (!valid(tuning.humanReplyWaitSeconds, 10, 3600)) errors.push("留给群友回答的时间必须在 10–3600 秒之间。");
  if (!valid(tuning.sessionTimeoutSeconds, 60, 86400)) errors.push("个人上下文保留时间必须在 60–86400 秒之间。");
  if (!valid(tuning.maxConcurrency, 1, 20)) errors.push("同时检索问题数必须在 1–20 之间。");
  if (!valid(tuning.mcpTimeoutSeconds, 60, 1800)) errors.push("单个问题 MCP 最长等待必须在 60–1800 秒之间。");
  return errors;
}

export function buildListenerSaveBody(input: {
  draft: ListenerDraftState;
  toolGrants: ListenerToolGrant[];
  webhookEdit: SecretEdit;
}): ReplyRuntimeCommandBody {
  const { draft } = input;
  return {
    kind: "listener.save",
    listener: {
      ...(draft.id ? { id: draft.id } : {}),
      name: draft.name.trim(),
      enabled: draft.enabled,
      groupId: draft.groupId,
      groupName: draft.groupName,
      toolGrants: input.toolGrants.map(({ serverId, toolName, schemaSha256 }) => ({ serverId, toolName, schemaSha256 })),
      systemPrompt: draft.systemPrompt.trim(),
      ...draft.tuning,
      autoSend: draft.deliveryMode === "automatic",
    },
    secretPatch: { webhookUrl: input.webhookEdit },
  };
}

export type WorkActionKind = "work.send" | "work.send_plain_at" | "work.discard";

export function buildWorkActionBody(
  kind: WorkActionKind,
  workId: string,
  expectedVersion: number,
  confirmedNotDelivered = false,
): ReplyRuntimeCommandBody {
  return {
    kind,
    workId,
    expectedVersion,
    ...(kind === "work.send_plain_at" ? { acknowledgement: "PLAIN_AT_IS_NOT_A_TRUE_MENTION" } : {}),
    ...(kind !== "work.discard" && confirmedNotDelivered ? { confirmedNotDelivered: true } : {}),
  };
}

export const createCommand = <Body extends ReplyRuntimeCommandBody>(
  body: Body,
  expectedRevision?: number,
): ReplyRuntimeCommand<Body> => ({
  protocolVersion: 1,
  commandId: globalThis.crypto?.randomUUID?.() ?? `cmd-${Date.now()}-${Math.random().toString(16).slice(2)}`,
  ...(expectedRevision === undefined ? {} : { expectedRevision }),
  body,
});

export const createQuery = <Body extends ReplyRuntimeCommandBody>(body: Body): ReplyRuntimeQuery<Body> => ({
  protocolVersion: 1,
  body,
});

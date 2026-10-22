/**
 * Shared types, wire normalizers and presentation helpers for the group reply
 * page. Kept free of JSX so both the page shell and the extracted child
 * components can import it without cycles.
 */

import { defaultListenerDraft, type ListenerDraftState } from "../../lib/replyRuntimeUi";
import type {
  DifyAppSummary,
  ListenerToolGrant,
  ReplyListenerSummary,
  ReplyRuntimeSnapshot,
  ReplyWorkImageStatus,
  ReplyWorkItem,
  ReplyWorkStatus,
} from "../../types";

export type ViewMode = "listeners" | "active" | "pending" | "history";

export interface WorkFilters {
  search: string;
  answerEngine: "" | "mcp" | "dify";
  stage: string;
  stopReason: string;
  recovery: "" | "normal" | "recovered";
}

export const emptyWorkFilters = (): WorkFilters => ({
  search: "",
  answerEngine: "",
  stage: "",
  stopReason: "",
  recovery: "",
});

export const WORK_PAGE_SIZE = 30;

export interface WorkPageState {
  page: number;
  total: number;
}

export interface ListenerDraft extends ListenerDraftState {
  webhookMode: "keep" | "replace" | "clear";
}

export interface TestChallenge {
  listenerId: string;
  challengeId: string;
  code?: string;
  revision: number;
}

export const listenerDraft = (): ListenerDraft => ({
  ...defaultListenerDraft(),
  webhookMode: "replace",
});

export function collection<T>(value: unknown, keys: string[]): T[] {
  if (Array.isArray(value)) return value as T[];
  if (!value || typeof value !== "object") return [];
  const record = value as Record<string, unknown>;
  for (const key of keys) if (Array.isArray(record[key])) return record[key] as T[];
  return [];
}

export function difyAppOptionFromWire(raw: Record<string, unknown>): DifyAppSummary {
  const secrets = (raw.secrets ?? {}) as Record<string, unknown>;
  const lastTest = (raw.lastTest ?? {}) as Record<string, unknown>;
  return {
    id: String(raw.id ?? ""),
    revision: Number(raw.revision ?? 0),
    name: String(raw.name ?? "未命名 Chatflow"),
    enabled: Boolean(raw.enabled),
    baseUrl: String(raw.baseUrl ?? ""),
    inputs: raw.inputs && typeof raw.inputs === "object" && !Array.isArray(raw.inputs)
      ? raw.inputs as Record<string, unknown>
      : {},
    secrets: {
      apiKeyConfigured: Boolean(secrets.apiKeyConfigured),
      fingerprint: String(secrets.fingerprint ?? ""),
    },
    capabilities: raw.capabilities && typeof raw.capabilities === "object"
      ? raw.capabilities as DifyAppSummary["capabilities"]
      : undefined,
    lastTest: {
      status: ["success", "failed"].includes(String(lastTest.status))
        ? String(lastTest.status) as "success" | "failed"
        : "never",
      testedAt: String(lastTest.testedAt ?? "") || undefined,
      error: lastTest.error,
    },
    connectionTestCurrent: Boolean(raw.connectionTestCurrent),
    updatedAt: String(raw.updatedAt ?? "") || undefined,
    listenerReferenceCount: Number(raw.listenerReferenceCount ?? 0),
  };
}

const degradedListenerHealth = new Set([
  "warning",
  "tool_grant_invalidated",
  "server_unhealthy",
  "rediscovery_required",
  "tool_schema_changed",
  "missing_server",
  "missing_tool",
  "disabled_server",
  "tool_safety_blocked",
  "missing_dify_app",
  "disabled_dify_app",
  "dify_api_key_required",
  "dify_connection_test_required",
]);

const listenerHealthMessages: Record<string, string> = {
  tool_grant_invalidated: "已授权工具的权限因 MCP 配置或目录变化而失效，请重新测试服务并重新选择工具。",
  server_unhealthy: "MCP 服务最近一次连接测试失败；若已有有效工具目录，监听仍可继续运行，请稍后重新测试。",
  warning: "最近一次 MCP 连接测试失败；已保留有效工具目录，监听仍可继续运行。",
  rediscovery_required: "MCP 连接配置已变化，请重新测试服务以发现工具并重新确认授权。",
  tool_schema_changed: "已授权工具的 Schema 发生变化，请重新测试服务并重新确认授权。",
  missing_server: "已授权的 MCP 服务已不存在，请重新选择工具。",
  missing_tool: "已授权的 MCP 工具已不存在，请重新测试服务并重新选择工具。",
  disabled_server: "已授权的 MCP 服务已停用，请启用服务或更换工具。",
  tool_safety_blocked: "Agent v2 仅允许已确认的只读工具，请检查工具安全分类。",
  missing_dify_app: "所选 Dify 应用已不存在，请重新选择 Chatflow。",
  disabled_dify_app: "所选 Dify 应用已停用，请先在 Dify 接入中启用。",
  dify_api_key_required: "所选 Dify 应用尚未配置 API Key。",
  dify_connection_test_required: "所选 Dify 配置尚未通过当前连接测试。",
  ready: "运行条件已就绪",
  error: "监听运行异常，请刷新后检查 MCP 服务和监听配置。",
};

export function listenerFromWire(raw: Record<string, unknown>): ReplyListenerSummary {
  const webhook = (raw.webhook ?? {}) as Record<string, unknown>;
  const health = (raw.health ?? {}) as Record<string, unknown>;
  const healthStatus = String(health.status ?? "stopped");
  const healthDetail = typeof health.message === "string" ? health.message.trim() : "";
  const normalizedHealth: ReplyListenerSummary["health"] = healthStatus === "ready"
    ? raw.enabled ? "monitoring" : "stopped"
    : degradedListenerHealth.has(healthStatus)
      ? "degraded"
      : healthStatus === "error" ? "error" : "stopped";
  const grants = (raw.toolGrants ?? raw.tools ?? []) as ListenerToolGrant[];
  const answerEngine = String(raw.answerEngine ?? "mcp") === "dify" ? "dify" : "mcp";
  return {
    id: String(raw.id ?? ""),
    revision: Number(raw.revision ?? 0),
    name: String(raw.name ?? "未命名监听器"),
    enabled: Boolean(raw.enabled),
    groupId: String(raw.groupId ?? ""),
    groupName: String(raw.groupName ?? raw.groupId ?? "未知群聊"),
    answerEngine,
    runtimeVersion: "v2",
    difyAppId: answerEngine === "dify" ? String(raw.difyAppId ?? "") : "",
    systemPrompt: String(raw.systemPrompt ?? ""),
    webhookConfigured: Boolean(webhook.configured ?? raw.webhookConfigured),
    webhookHint: String(webhook.fingerprint ?? raw.webhookHint ?? ""),
    webhookVerified: Boolean(webhook.confirmed ?? raw.webhookVerified),
    webhookVerifiedAt: String(webhook.confirmedAt ?? raw.webhookVerifiedAt ?? ""),
    deliveryMode: Boolean(raw.autoSend) ? "automatic" : "review",
    requireIndependentReview: raw.requireIndependentReview !== false,
    reviewPrompt: String(raw.reviewPrompt ?? ""),
    tools: grants,
    tuning: {
      pollIntervalSeconds: Number(raw.pollIntervalSeconds ?? 5),
      sameSenderMergeSeconds: Number(raw.sameSenderMergeSeconds ?? 20),
      humanReplyWaitSeconds: Number(raw.humanReplyWaitSeconds ?? 120),
      sessionTimeoutSeconds: Number(raw.sessionTimeoutSeconds ?? 1800),
      maxConcurrency: Number(raw.maxConcurrency ?? 4),
      mcpTimeoutSeconds: Number(raw.mcpTimeoutSeconds ?? 900),
      difyTimeoutSeconds: Number(raw.difyTimeoutSeconds ?? 300),
      maxAgentRounds: Number(raw.maxAgentRounds ?? 6),
      maxToolCalls: Number(raw.maxToolCalls ?? 200),
    },
    health: normalizedHealth,
    healthMessage: (healthStatus === "error" || healthStatus === "warning") && healthDetail
      ? healthDetail
      : listenerHealthMessages[healthStatus]
      ?? String(health.message ?? healthStatus),
    pendingCount: Number(raw.pendingCount ?? 0),
    lastPollAt: String(raw.lastPollAt ?? ""),
    toolSafety: raw.toolSafety && typeof raw.toolSafety === "object"
      ? raw.toolSafety as ReplyListenerSummary["toolSafety"]
      : undefined,
    lastRun: raw.lastRun && typeof raw.lastRun === "object"
      ? raw.lastRun as ReplyListenerSummary["lastRun"]
      : undefined,
    recoverableCount: Number(raw.recoverableCount ?? 0),
  };
}

export function listenerSaveResultFromWire(value: unknown): {
  listener: Record<string, unknown>;
  revision: number;
} {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("后台保存响应格式无效，请刷新后重试。");
  }
  const result = value as Record<string, unknown>;
  if (!result.listener || typeof result.listener !== "object" || Array.isArray(result.listener)) {
    throw new Error("后台保存成功，但没有返回监听器状态");
  }
  const listener = result.listener as Record<string, unknown>;
  const revision = result.revision ?? listener.revision;
  if (typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 0) {
    throw new Error("后台保存成功，但返回的配置版本无效");
  }
  return { listener, revision };
}

function normalizeWorkStatus(value: string): ReplyWorkStatus {
  if (["collecting", "classifying", "waiting_for_image", "waiting_for_human_reply", "queued_retrieval"].includes(value)) return "waiting";
  if (["retrieving", "reviewing", "ready_to_send", "sending", "queued_delivery"].includes(value)) return "working";
  if (["pending", "awaiting_review", "delivery_unknown", "delivery_failed", "needs_image"].includes(value)) return "pending";
  if (value === "sent") return "sent";
  if (["failed", "mcp_timeout"].includes(value)) return "failed";
  return "closed";
}

const workImageStatuses = new Set<ReplyWorkImageStatus>([
  "none", "resolving", "ready", "processed", "partial", "unavailable", "unsupported",
]);

export function workFromWire(raw: Record<string, unknown>): ReplyWorkItem {
  const identity = (raw.identity ?? {}) as Record<string, unknown>;
  const mention = (raw.mention ?? {}) as Record<string, unknown>;
  const rawError = (raw.error ?? {}) as Record<string, unknown>;
  const question = raw.question;
  const sourceDelaySeconds = Number(raw.sourceDelaySeconds ?? raw.source_delay_seconds);
  return {
    id: String(raw.id ?? raw.workId ?? ""),
    version: Number(raw.version ?? raw.generation ?? 0),
    listenerId: String(raw.listenerId ?? ""),
    listenerName: String(raw.listenerName ?? ""),
    answerEngine: String(raw.answerEngine ?? "") === "dify" ? "dify" : String(raw.answerEngine ?? "") === "mcp" ? "mcp" : undefined,
    runtimeVersion: "v2",
    groupId: String(raw.groupId ?? ""),
    groupName: String(raw.groupName ?? raw.groupId ?? "未知群聊"),
    senderId: String(raw.senderId ?? ""),
    senderName: String(raw.senderName ?? identity.displayName ?? raw.senderId ?? "未知成员"),
    question: typeof question === "string"
      ? question
      : String((question as Record<string, unknown> | undefined)?.text ?? raw.questionText ?? ""),
    answer: String(raw.answer ?? ""),
    status: normalizeWorkStatus(String(raw.status ?? "")),
    stage: String(raw.status ?? raw.stage ?? ""),
    reason: String(raw.reason ?? raw.pendingReason ?? raw.closeReason ?? rawError.message ?? ""),
    errorCode: String(rawError.code ?? "") || undefined,
    errorStage: String(rawError.stage ?? "") || undefined,
    mentionMode: (raw.mentionMode ?? identity.mentionMode
      ?? (mention.accountConfigured ? "userid" : mention.mobileConfigured ? "mobile" : "unresolved")) as ReplyWorkItem["mentionMode"],
    createdAt: String(raw.createdAt ?? raw.created_at ?? ""),
    updatedAt: String(raw.updatedAt ?? raw.updated_at ?? ""),
    completedAt: String(raw.completedAt ?? raw.completed_at ?? "") || undefined,
    detectedAt: String(raw.detectedAt ?? raw.detected_at ?? "") || undefined,
    sourceDelaySeconds: Number.isFinite(sourceDelaySeconds) && sourceDelaySeconds >= 0
      ? sourceDelaySeconds
      : undefined,
    mergeDueAt: String(raw.mergeDueAt ?? raw.merge_due_at ?? "") || undefined,
    humanWaitDueAt: String(raw.humanWaitDueAt ?? raw.human_wait_due_at ?? "") || undefined,
    imageRetryAt: String(raw.imageRetryAt ?? raw.image_retry_at ?? "") || undefined,
    imageWaitDueAt: String(raw.imageWaitDueAt ?? raw.image_wait_due_at ?? "") || undefined,
    imageCount: Math.max(0, Number(raw.imageCount ?? raw.image_count ?? 0) || 0),
    imageAvailableCount: Math.max(0, Number(raw.imageAvailableCount ?? raw.image_available_count ?? 0) || 0),
    imageUnavailableCount: Math.max(0, Number(raw.imageUnavailableCount ?? raw.image_unavailable_count ?? 0) || 0),
    fileCount: Math.max(0, Number(raw.fileCount ?? raw.file_count ?? 0) || 0),
    fileAvailableCount: Math.max(0, Number(raw.fileAvailableCount ?? raw.file_available_count ?? 0) || 0),
    fileUnavailableCount: Math.max(0, Number(raw.fileUnavailableCount ?? raw.file_unavailable_count ?? 0) || 0),
    imageStatus: workImageStatuses.has(String(raw.imageStatus ?? raw.image_status ?? "none") as ReplyWorkImageStatus)
      ? String(raw.imageStatus ?? raw.image_status ?? "none") as ReplyWorkImageStatus
      : "none",
    duplicateCount: Math.max(0, Number(raw.duplicateCount ?? raw.duplicate_count ?? 0) || 0),
    stopReason: String(raw.stopReason ?? "") || undefined,
    recoveryState: String(raw.recoveryState ?? "") || undefined,
    providerAudit: raw.providerAudit && typeof raw.providerAudit === "object"
      ? raw.providerAudit as Record<string, unknown>
      : undefined,
    usage: raw.usage && typeof raw.usage === "object"
      ? raw.usage as ReplyWorkItem["usage"]
      : undefined,
    progress: raw.progress && typeof raw.progress === "object"
      ? raw.progress as ReplyWorkItem["progress"]
      : undefined,
    timeline: collection<Record<string, unknown>>(raw.timeline, ["items"]).map((entry) => ({
      workGeneration: Number(entry.workGeneration ?? 0),
      sequence: Number(entry.sequence ?? 0),
      role: String(entry.role ?? "runtime"),
      kind: String(entry.kind ?? "event"),
      metadata: entry.metadata && typeof entry.metadata === "object"
        ? entry.metadata as Record<string, unknown>
        : {},
      detail: entry.detail && typeof entry.detail === "object"
        ? entry.detail as Record<string, unknown>
        : null,
      createdAt: String(entry.createdAt ?? "") || undefined,
    })),
    evidence: collection<Record<string, unknown>>(raw.evidence, ["items"]).map((entry) => ({
      provider: String(entry.provider ?? "") === "dify" ? "dify" : "mcp",
      serverName: String(entry.serverName ?? entry.serverId ?? ""),
      toolName: String(entry.toolName ?? ""),
      summary: String(entry.summary ?? (entry.result as Record<string, unknown> | undefined)?.content ?? "已取得检索证据"),
    })),
  };
}

export function snapshotFromWire(raw: Record<string, unknown>): ReplyRuntimeSnapshot {
  const runtime = (raw.runtime ?? raw) as Record<string, unknown>;
  const work = (raw.work ?? raw) as Record<string, unknown>;
  return {
    running: Boolean(runtime.running ?? raw.running),
    startedAt: String(runtime.startedAt ?? raw.startedAt ?? ""),
    activeRetrievals: Number(work.activeRetrievals ?? work.active ?? raw.activeRetrievals ?? 0),
    queuedRetrievals: Number(work.queuedRetrievals ?? work.queued ?? raw.queuedRetrievals ?? 0),
    pendingCount: Number(work.pendingCount ?? work.pending ?? raw.pendingCount ?? 0),
    recentFailures: Number(work.recentFailures ?? work.failures ?? raw.recentFailures ?? 0),
  };
}

export const healthCopy = (health: ReplyListenerSummary["health"]) => {
  if (health === "monitoring") return { label: "监听中", tone: "success" };
  if (health === "degraded") return { label: "需要处理", tone: "warning" };
  if (health === "error") return { label: "运行异常", tone: "danger" };
  if (health === "starting") return { label: "启动中", tone: "progress" };
  return { label: "已停止", tone: "neutral" };
};

export const dateLabel = (value?: string) => {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
};

const terminalWorkAnswerCopy: Record<string, string> = {
  answered_by_human: "群友已在等待期内回复，本次无需生成回答。",
  ignored_non_question: "这条消息未被识别为问题，本次未生成回答。",
  ignored_unsupported: "这类消息暂不支持处理，本次未生成回答。",
  skipped_no_evidence: "MCP 未返回足够证据，本次未生成回答。",
  skipped_empty_answer: "模型未生成有效内容，本次未生成回答。",
  skipped_review_failed: "独立审核未通过，本次未生成回答。",
  skipped_image_unavailable: "图片无法读取，本次未生成回答。",
  skipped_image_unsupported: "当前模型不支持图片识别，本次未生成回答。",
  withdrawn: "提问已撤回，本次未生成回答。",
  discarded: "已由操作员放弃发送。",
  closed_configuration_changed: "监听配置发生变化，任务已关闭。",
  closed_runtime_restarted: "后台运行模块重启，任务已关闭。",
  mcp_timeout: "MCP 检索超时，本次未生成回答。",
  failed: "处理失败，本次未生成回答。",
  closed: "本次未生成回答。",
};

const terminalWorkErrorCopy: Record<string, string> = {
  CLASSIFICATION_FAILED: "消息分类失败，本次未进入群友等待或 MCP 检索。",
  MCP_PREFLIGHT_FAILED: "MCP 会话预检失败，重连后仍无法开始工具调用。",
  MCP_SESSION_INTERRUPTED: "MCP 工具请求发出后会话中断；为避免重复执行，系统未自动重放。",
  MCP_TOOL_ERROR: "MCP 工具返回错误，本次未生成回答。",
  MCP_TIMEOUT: "MCP 检索超时，本次未生成回答。",
  MCP_OPERATION_FAILED: "MCP 操作失败，本次未生成回答。",
  DIFY_TIMEOUT: "Dify Chatflow 超时，本次未生成回答；可由人工重试。",
  DIFY_STREAM_INCOMPLETE: "Dify Chatflow 响应中断，本次未生成回答；可由人工重试。",
  DIFY_WORKFLOW_FAILED: "Dify Chatflow 工作流失败，本次未生成回答。",
  DIFY_WORKFLOW_PAUSED: "Dify Chatflow 等待人工输入，当前流程不支持自动续接，本次未生成回答。",
  DIFY_FILE_UNSUPPORTED: "Dify 不支持本轮附件格式，本次未生成回答。",
  DIFY_FILE_TOO_LARGE: "Dify 拒绝了过大的附件，本次未生成回答。",
  RETRIEVAL_FAILED: "检索或回答流程失败，本次未生成回答。",
  RUNTIME_ADAPTER_UNAVAILABLE: "模型或 MCP 适配器不可用，本次未生成回答。",
  MODEL_NOT_CONFIGURED: "模型尚未配置，本次未生成回答。",
  MODEL_NETWORK_ERROR: "模型服务请求失败，本次未生成回答。",
  RUNTIME_SHUTDOWN: "后台运行模块关闭，任务已终止。",
};

export const timelineRoleCopy: Record<string, string> = {
  classifier: "问题分类",
  human_reply_matcher: "人工回答判断",
  retriever: "检索 Agent",
  composer: "答案编排",
  compressor: "答案压缩",
  reviewer: "独立复核",
  delivery: "发送",
  runtime: "运行时",
};

export const timelineKindCopy: Record<string, string> = {
  run_started: "开始执行",
  run_finished: "执行结束",
  agent_started: "Agent 开始",
  agent_finished: "Agent 停止",
  model_decision_started: "模型决策",
  model_decision_completed: "模型决策完成",
  bootstrap_started: "首轮必查开始",
  bootstrap_tool_started: "调用首轮必查工具",
  bootstrap_tool_completed: "首轮必查完成",
  bootstrap_tool_failed: "首轮必查失败",
  tool_call_started: "调用 MCP 工具",
  tool_call_completed: "工具调用完成",
  tool_call_failed: "工具调用失败",
  tool_call_cache_hit: "复用相同调用结果",
  tool_call_invalid_arguments: "工具参数无效",
  tool_call_skipped: "工具调用已跳过",
  empty_evidence_reprompt: "空结果改写查询",
  agent_stalled: "检索停滞",
  retrieval_watchdog_checked: "检索看门狗检查",
  context_compaction_completed: "上下文已压缩",
  answer_generation_started: "开始生成答案",
  answer_generation_completed: "答案生成完成",
  answer_compression_started: "开始压缩答案",
  answer_compression_completed: "答案压缩完成",
  independent_review_started: "开始独立复核",
  independent_review_completed: "独立复核完成",
  recovery_scheduled: "任务恢复排队",
  manual_retry_scheduled: "人工重试排队",
  run_error: "运行失败",
};

export function timelineGroups(item: ReplyWorkItem) {
  const groups = new Map<string, NonNullable<ReplyWorkItem["timeline"]>>();
  for (const event of item.timeline ?? []) {
    const entries = groups.get(event.role) ?? [];
    entries.push(event);
    groups.set(event.role, entries);
  }
  return [...groups.entries()];
}

export const retryableWorkStages = new Set([
  "failed",
  "skipped_no_evidence",
  "skipped_empty_answer",
  "skipped_review_failed",
  "closed_runtime_restarted",
]);

const genericTerminalWorkStages = new Set(["failed", "closed"]);
const imageTerminalWorkStages = new Set([
  "skipped_image_unavailable",
  "skipped_image_unsupported",
]);
const imageTerminalErrorCodes = new Set([
  "IMAGE_FILE_MISSING",
  "IMAGE_TOO_LARGE",
  "IMAGE_UNREADABLE",
  "MODEL_VISION_UNSUPPORTED",
]);

export function workAnswerCopy(item: ReplyWorkItem): string {
  // Older databases may contain the rejected draft from before v3.2.17.
  // Never surface an answer that failed the independent evidence review.
  if (item.stage === "skipped_review_failed") return terminalWorkAnswerCopy.skipped_review_failed;
  if (item.reason === "image_download_timeout"
    || (item.errorStage === "waiting_for_image" && item.errorCode === "IMAGE_FILE_MISSING")) {
    return "图片尚未写入本机缓存";
  }
  if (item.answer?.trim()) return item.answer.trim();
  if (item.status === "waiting" || item.status === "working") return "回答仍在生成中";
  if (item.stage === "skipped_no_evidence" && item.answerEngine === "dify") {
    return "Dify 未返回可复核的有效回答，本次未生成回答。";
  }
  const errorOutcome = terminalWorkErrorCopy[item.errorCode ?? ""];
  if (errorOutcome) return errorOutcome;
  const outcome = terminalWorkAnswerCopy[item.stage ?? ""]
    ?? terminalWorkAnswerCopy[item.reason ?? ""];
  if (outcome && !genericTerminalWorkStages.has(item.stage ?? "")) return outcome;
  const imageTerminated = imageTerminalWorkStages.has(item.stage ?? "")
    || imageTerminalErrorCodes.has(item.errorCode ?? "");
  if (item.reason?.trim() && !imageTerminated) return item.reason.trim();
  if (item.imageStatus === "unsupported") return "当前模型不支持图片识别，本次未生成回答。";
  if (item.imageStatus === "unavailable") return "图片无法读取，本次未生成回答。";
  if (outcome) return outcome;
  if (item.reason?.trim()) return item.reason.trim();
  return "本次未生成回答。";
}

const activeWorkStageCopy: Record<string, string> = {
  collecting: "等待连续补充",
  classifying: "正在识别问题",
  waiting_for_image: "等待图片写入本机缓存",
  waiting_for_human_reply: "等待群友回复",
  queued_retrieval: "等待 MCP 检索",
  retrieving: "MCP 检索中",
  reviewing: "正在审核回答",
  ready_to_send: "准备发送",
  sending: "正在发送",
  queued_delivery: "等待发送",
  needs_image: "需要重新读取图片",
};

export function workStatusCopy(item: ReplyWorkItem): string {
  if (item.stage === "needs_image") return "需要图片";
  if (item.stage === "waiting_for_image"
    || ["waiting_for_image", "waiting_for_wecom_image_cache"].includes(item.reason ?? "")) {
    return (item.fileCount ?? 0) > 0
      ? "等待图片或附件写入本机缓存"
      : "等待图片写入本机缓存";
  }
  if (item.status === "pending") return "待发送";
  if (item.status === "sent") return "已发送";
  if (item.status === "failed") return "失败";
  if (item.status === "closed") return "已结束";
  if (item.stage === "queued_retrieval") return item.answerEngine === "dify" ? "等待 Dify Chatflow" : "等待 MCP 检索";
  if (item.stage === "retrieving") return item.answerEngine === "dify" ? "Dify Chatflow 处理中" : "MCP 检索中";
  return activeWorkStageCopy[item.stage ?? ""] ?? "处理中";
}

export interface WorkStageStep {
  key: "detected" | "merge" | "image" | "human" | "mcp";
  label: string;
  state: "complete" | "current" | "upcoming" | "skipped" | "failed";
  deadline?: string;
}

export function workStageStepCopy(step: WorkStageStep): string {
  if (step.state === "skipped") return "无需执行";
  if (step.state === "failed") return "处理失败";
  if (step.deadline) {
    return `${step.key === "detected" ? "发现于" : "截止"} ${dateLabel(step.deadline)}`;
  }
  if (step.state === "current") return "正在处理";
  if (step.state === "complete") return "已完成";
  return "尚未开始";
}

export function workStageTimeline(item: ReplyWorkItem): WorkStageStep[] {
  const rawStage = item.stage ?? "";
  const engineLabel = item.answerEngine === "dify" ? "Dify Chatflow" : "MCP 检索";
  const attachmentWaitLabel = (item.fileCount ?? 0) > 0
    ? "等待图片或附件写入本机缓存"
    : "等待图片写入本机缓存";
  if (rawStage === "needs_image") {
    return [
      { key: "detected", label: "发现消息", deadline: item.detectedAt, state: "complete" },
      { key: "merge", label: "等待连续补充", deadline: item.mergeDueAt, state: "complete" },
      { key: "image", label: "读取图片", deadline: item.imageWaitDueAt, state: "failed" },
      { key: "human", label: "等待群友", deadline: item.humanWaitDueAt, state: "skipped" },
      { key: "mcp", label: engineLabel, deadline: undefined, state: "skipped" },
    ];
  }
  const imageWaitActive = rawStage === "waiting_for_image"
    || ["waiting_for_image", "waiting_for_wecom_image_cache"].includes(item.reason ?? "");
  if (imageWaitActive) {
    return [
      { key: "detected", label: "发现消息", deadline: item.detectedAt, state: "complete" },
      { key: "merge", label: "等待连续补充", deadline: item.mergeDueAt, state: "complete" },
      { key: "image", label: attachmentWaitLabel, deadline: item.imageWaitDueAt, state: "current" },
      { key: "human", label: "等待群友", deadline: item.humanWaitDueAt, state: "upcoming" },
      { key: "mcp", label: engineLabel, deadline: undefined, state: "upcoming" },
    ];
  }
  let currentIndex = 4;
  if (item.status === "waiting") {
    currentIndex = rawStage === "waiting_for_human_reply" ? 2
      : rawStage === "queued_retrieval" ? 3
        : rawStage === "classifying" ? 0 : 1;
  } else if (item.status === "working") {
    currentIndex = ["ready_to_send", "sending", "queued_delivery"].includes(rawStage)
      ? 4
      : 3;
  }

  const definitions = [
    { key: "detected" as const, label: "发现消息", deadline: item.detectedAt },
    { key: "merge" as const, label: "等待连续补充", deadline: item.mergeDueAt },
    { key: "human" as const, label: "等待群友", deadline: item.humanWaitDueAt },
    { key: "mcp" as const, label: engineLabel, deadline: undefined },
  ];

  if (currentIndex !== 4) {
    return definitions.map((step, index) => ({
      ...step,
      state: index < currentIndex ? "complete"
        : index === currentIndex ? "current" : "upcoming",
    }));
  }

  if (rawStage === "failed") {
    if (["queued_retrieval", "retrieving"].includes(item.errorStage ?? "")) {
      return definitions.map((step, index) => ({
        ...step,
        state: index < 3 ? "complete" : "failed",
      }));
    }
    const completedThrough = item.errorStage === "collecting" ? 1 : 0;
    return definitions.map((step, index) => ({
      ...step,
      state: index <= completedThrough ? "complete" : "skipped",
    }));
  }

  if (["closed_configuration_changed", "closed_runtime_restarted"].includes(rawStage)) {
    const interruptedIndexByStage: Record<string, number> = {
      collecting: 1,
      waiting_for_human_reply: 2,
      queued_retrieval: 3,
      retrieving: 3,
      ready_to_send: 4,
      pending: 4,
      delivery_failed: 4,
    };
    const interruptedIndex = interruptedIndexByStage[item.errorStage ?? ""] ?? 1;
    return definitions.map((step, index) => ({
      ...step,
      state: index < interruptedIndex ? "complete" : "skipped",
    }));
  }

  const completedAfterMergeDeadline = (() => {
    if (!item.completedAt || !item.mergeDueAt) return false;
    const completedAt = new Date(item.completedAt).getTime();
    const mergeDueAt = new Date(item.mergeDueAt).getTime();
    return Number.isFinite(completedAt) && Number.isFinite(mergeDueAt) && completedAt >= mergeDueAt;
  })();
  const terminalCompletedThrough = ["skipped_image_unavailable", "skipped_image_unsupported"]
    .includes(rawStage) ? (completedAfterMergeDeadline ? 1 : 0)
    : rawStage === "ignored_unsupported" ? 0
    : ["ignored_non_question", "withdrawn"].includes(rawStage) ? 1
      : rawStage === "answered_by_human" ? 2
        : 3;
  return definitions.map((step, index) => ({
    ...step,
    state: index <= terminalCompletedThrough ? "complete" : "skipped",
  }));
}

export function imageStatusCopy(
  status: ReplyWorkImageStatus = "none",
  count = 0,
  availableCount = 0,
  unavailableCount = 0,
): string {
  const amount = Math.max(0, count);
  if (status === "resolving") return "等待图片写入本机缓存";
  if (status === "processed") return `${amount || 1} 张图片已识别并用于回答`;
  if (status === "ready") return `${amount || 1} 张图片已读取并提供给模型`;
  if (status === "partial") {
    return `${Math.max(0, availableCount)} 张已读取，${Math.max(0, unavailableCount)} 张无法读取；回答仅参考已读取图片`;
  }
  if (status === "unavailable") return `${amount || 1} 张图片无法读取`;
  if (status === "unsupported") return amount === 1
    ? "当前模型不支持识别这张图片"
    : `当前模型不支持识别这 ${amount || 1} 张图片`;
  return "未附带图片";
}

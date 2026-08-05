import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleOff,
  Clock3,
  Edit3,
  Eye,
  Gauge,
  History,
  Inbox,
  LoaderCircle,
  MessageCircleQuestion,
  MessageSquareReply,
  Minus,
  Network,
  Plus,
  RefreshCw,
  Save,
  Search,
  Send,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Trash2,
  UserRound,
  UsersRound,
  Webhook,
  Wrench,
  X,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import { Button, Field, Input, SectionHeader, Switch } from "../components/ui";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import {
  applyGroupReplyRuntimeEvent,
  createGroupReplyEventRefreshScheduler,
  createSelectedWorkDetailRefresher,
} from "../lib/groupReplyEventRefresh";
import {
  automaticDeliveryBlockers,
  buildListenerSaveBody,
  buildWorkActionBody,
  createCommand,
  createQuery,
  defaultListenerDraft,
  executeListenerSave,
  isMcpToolGrantable,
  mcpCatalogAllowsGrants,
  mcpServerGrantState,
  runtimeAvailabilityCopy,
  toggleMcpServerGrant,
  toggleToolSelection,
  toolGrantFromSelectionKey,
  toolGrantSelectionKey,
  tuningValidationErrors,
  type ListenerDraftState,
  type WorkActionKind,
} from "../lib/replyRuntimeUi";
import type {
  GroupInfo,
  ListenerToolGrant,
  McpServerSummary,
  McpToolSummary,
  ReplyListenerSummary,
  ReplyRuntimeEvent,
  ReplyRuntimeSnapshot,
  ReplyWorkItem,
  ReplyWorkImageStatus,
  ReplyWorkStatus,
} from "../types";

type ViewMode = "listeners" | "active" | "pending" | "history";

const WORK_PAGE_SIZE = 30;

interface WorkPageState {
  page: number;
  total: number;
}

interface ToolChoice extends McpToolSummary {
  key: string;
  serverId: string;
  serverName: string;
}

interface McpCatalogEntry {
  serverId: string;
  tools: McpToolSummary[];
  error?: unknown;
}

interface ToolGroup {
  serverId: string;
  serverName: string;
  tools: ToolChoice[];
}

type McpServerWithCatalog = McpServerSummary & {
  catalog?: { error?: unknown };
};

interface ListenerDraft extends ListenerDraftState {
  webhookMode: "keep" | "replace" | "clear";
}

const listenerDraft = (): ListenerDraft => ({ ...defaultListenerDraft(), webhookMode: "replace" });

function collection<T>(value: unknown, keys: string[]): T[] {
  if (Array.isArray(value)) return value as T[];
  if (!value || typeof value !== "object") return [];
  const record = value as Record<string, unknown>;
  for (const key of keys) if (Array.isArray(record[key])) return record[key] as T[];
  return [];
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
  return {
    id: String(raw.id ?? ""),
    revision: Number(raw.revision ?? 0),
    name: String(raw.name ?? "未命名监听器"),
    enabled: Boolean(raw.enabled),
    groupId: String(raw.groupId ?? ""),
    groupName: String(raw.groupName ?? raw.groupId ?? "未知群聊"),
    systemPrompt: String(raw.systemPrompt ?? ""),
    webhookConfigured: Boolean(webhook.configured ?? raw.webhookConfigured),
    webhookHint: String(webhook.fingerprint ?? raw.webhookHint ?? ""),
    webhookVerified: Boolean(webhook.confirmed ?? raw.webhookVerified),
    webhookVerifiedAt: String(webhook.confirmedAt ?? raw.webhookVerifiedAt ?? ""),
    deliveryMode: Boolean(raw.autoSend) ? "automatic" : "review",
    tools: grants,
    tuning: {
      pollIntervalSeconds: Number(raw.pollIntervalSeconds ?? 5),
      sameSenderMergeSeconds: Number(raw.sameSenderMergeSeconds ?? 20),
      humanReplyWaitSeconds: Number(raw.humanReplyWaitSeconds ?? 120),
      sessionTimeoutSeconds: Number(raw.sessionTimeoutSeconds ?? 1800),
      maxConcurrency: Number(raw.maxConcurrency ?? 4),
      mcpTimeoutSeconds: Number(raw.mcpTimeoutSeconds ?? 900),
    },
    health: normalizedHealth,
    healthMessage: (healthStatus === "error" || healthStatus === "warning") && healthDetail
      ? healthDetail
      : listenerHealthMessages[healthStatus]
      ?? String(health.message ?? healthStatus),
    pendingCount: Number(raw.pendingCount ?? 0),
    lastPollAt: String(raw.lastPollAt ?? ""),
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
    imageStatus: workImageStatuses.has(String(raw.imageStatus ?? raw.image_status ?? "none") as ReplyWorkImageStatus)
      ? String(raw.imageStatus ?? raw.image_status ?? "none") as ReplyWorkImageStatus
      : "none",
    duplicateCount: Math.max(0, Number(raw.duplicateCount ?? raw.duplicate_count ?? 0) || 0),
    evidence: collection<Record<string, unknown>>(raw.evidence, ["items"]).map((entry) => ({
      serverName: String(entry.serverName ?? entry.serverId ?? ""),
      toolName: String(entry.toolName ?? ""),
      summary: String(entry.summary ?? (entry.result as Record<string, unknown> | undefined)?.content ?? "已取得检索证据"),
    })),
  };
}

function snapshotFromWire(raw: Record<string, unknown>): ReplyRuntimeSnapshot {
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

const healthCopy = (health: ReplyListenerSummary["health"]) => {
  if (health === "monitoring") return { label: "监听中", tone: "success" };
  if (health === "degraded") return { label: "需要处理", tone: "warning" };
  if (health === "error") return { label: "运行异常", tone: "danger" };
  if (health === "starting") return { label: "启动中", tone: "progress" };
  return { label: "已停止", tone: "neutral" };
};

const dateLabel = (value?: string) => {
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
  RETRIEVAL_FAILED: "检索或回答流程失败，本次未生成回答。",
  RUNTIME_ADAPTER_UNAVAILABLE: "模型或 MCP 适配器不可用，本次未生成回答。",
  MODEL_NOT_CONFIGURED: "模型尚未配置，本次未生成回答。",
  MODEL_NETWORK_ERROR: "模型服务请求失败，本次未生成回答。",
  RUNTIME_SHUTDOWN: "后台运行模块关闭，任务已终止。",
};

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
    return "图片尚未下载到本机";
  }
  if (item.answer?.trim()) return item.answer.trim();
  if (item.status === "waiting" || item.status === "working") return "回答仍在生成中";
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
  waiting_for_image: "等待企业微信下载图片",
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
    return "等待企业微信下载图片";
  }
  if (item.status === "pending") return "待发送";
  if (item.status === "sent") return "已发送";
  if (item.status === "failed") return "失败";
  if (item.status === "closed") return "已结束";
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
  if (rawStage === "needs_image") {
    return [
      { key: "detected", label: "发现消息", deadline: item.detectedAt, state: "complete" },
      { key: "merge", label: "等待连续补充", deadline: item.mergeDueAt, state: "complete" },
      { key: "image", label: "读取图片", deadline: item.imageWaitDueAt, state: "failed" },
      { key: "human", label: "等待群友", deadline: item.humanWaitDueAt, state: "skipped" },
      { key: "mcp", label: "MCP 检索", deadline: undefined, state: "skipped" },
    ];
  }
  const imageWaitActive = rawStage === "waiting_for_image"
    || ["waiting_for_image", "waiting_for_wecom_image_cache"].includes(item.reason ?? "");
  if (imageWaitActive) {
    return [
      { key: "detected", label: "发现消息", deadline: item.detectedAt, state: "complete" },
      { key: "merge", label: "等待连续补充", deadline: item.mergeDueAt, state: "complete" },
      { key: "image", label: "等待企业微信下载图片", deadline: item.imageWaitDueAt, state: "current" },
      { key: "human", label: "等待群友", deadline: item.humanWaitDueAt, state: "upcoming" },
      { key: "mcp", label: "MCP 检索", deadline: undefined, state: "upcoming" },
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
    { key: "mcp" as const, label: "MCP 检索", deadline: undefined },
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
  if (status === "resolving") return "等待企业微信下载图片";
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

export function GroupReplyPage() {
  const [view, setView] = useState<ViewMode>("listeners");
  const [listeners, setListeners] = useState<ReplyListenerSummary[]>([]);
  const [works, setWorks] = useState<ReplyWorkItem[]>([]);
  const [activePage, setActivePage] = useState<WorkPageState>({ page: 1, total: 0 });
  const [pendingPage, setPendingPage] = useState<WorkPageState>({ page: 1, total: 0 });
  const [historyPage, setHistoryPage] = useState<WorkPageState>({ page: 1, total: 0 });
  const [groups, setGroups] = useState<GroupInfo[]>([]);
  const [tools, setTools] = useState<ToolChoice[]>([]);
  const [snapshot, setSnapshot] = useState<ReplyRuntimeSnapshot>({});
  const [runtimeRevision, setRuntimeRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [editorOpen, setEditorOpen] = useState(false);
  const [draft, setDraft] = useState<ListenerDraft>(listenerDraft);
  const [groupSearch, setGroupSearch] = useState("");
  const [persistedGroupId, setPersistedGroupId] = useState("");
  const [expandedMcpServers, setExpandedMcpServers] = useState<string[]>([]);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [testChallenge, setTestChallenge] = useState<{ listenerId: string; challengeId: string; code?: string; revision: number } | null>(null);
  const [detail, setDetail] = useState<ReplyWorkItem | null>(null);
  const loadSequence = useRef(0);
  const foregroundLoadSequence = useRef(0);
  const editorOptionsLoadSequence = useRef(0);
  const detailRefresher = useRef<ReturnType<typeof createSelectedWorkDetailRefresher<ReplyWorkItem>> | null>(null);
  if (!detailRefresher.current) {
    detailRefresher.current = createSelectedWorkDetailRefresher((nextDetail) => setDetail(nextDetail));
  }

  const queryWorkDetail = useCallback(async (workId: string): Promise<ReplyWorkItem> => {
    const result = await bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "work.detail", workId }));
    const raw = (result.item ?? result.work ?? result) as Record<string, unknown>;
    return workFromWire(raw);
  }, []);

  const refreshOpenWorkDetail = useCallback(async () => {
    const selectedId = detailRefresher.current?.currentId();
    if (!selectedId) return;
    try {
      await detailRefresher.current?.refresh(selectedId, () => queryWorkDetail(selectedId));
    } catch {
      // Keep the last usable detail while the runtime moves between states.
    }
  }, [queryWorkDetail]);

  const closeWorkDetail = useCallback(() => {
    detailRefresher.current?.clear();
    setDetail(null);
  }, []);

  const load = useCallback(async (quiet = false) => {
    const sequence = ++loadSequence.current;
    const foregroundSequence = quiet ? undefined : ++foregroundLoadSequence.current;
    if (!quiet) setLoading(true);
    try {
      const [listenerResult, activeResult, pendingResult, historyResult, snapshotResult] = await Promise.allSettled([
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "listener.list" })),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({
          kind: "work.list",
          bucket: "active",
          page: activePage.page,
          limit: WORK_PAGE_SIZE,
        })),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({
          kind: "work.list",
          bucket: "pending",
          page: pendingPage.page,
          limit: WORK_PAGE_SIZE,
        })),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({
          kind: "work.list",
          bucket: "history",
          page: historyPage.page,
          limit: WORK_PAGE_SIZE,
        })),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "runtime.snapshot" })),
      ]);
      if (sequence !== loadSequence.current) return;
      if (snapshotResult.status === "fulfilled") setSnapshot(snapshotFromWire(snapshotResult.value));
      else setSnapshot({});
      if (listenerResult.status === "rejected") throw listenerResult.reason;
      if (activeResult.status === "rejected") throw activeResult.reason;
      if (pendingResult.status === "rejected") throw pendingResult.reason;
      if (historyResult.status === "rejected") throw historyResult.reason;
      const rawListeners = collection<Record<string, unknown>>(listenerResult.value, ["listeners", "items"]);
      const nextListeners = rawListeners.map(listenerFromWire);
      setListeners(nextListeners);
      setRuntimeRevision(Number(listenerResult.value.revision ?? 0));
      const listenerMap = new Map(nextListeners.map((listener) => [listener.id, listener]));
      const activeItems = collection<Record<string, unknown>>(activeResult.value, ["items", "works"]);
      const pendingItems = collection<Record<string, unknown>>(pendingResult.value, ["items", "works"]);
      const historyItems = collection<Record<string, unknown>>(historyResult.value, ["items", "works"]);
      const workItems = [...activeItems, ...pendingItems, ...historyItems];
      setWorks(workItems.map(workFromWire).map((item) => ({
          ...item,
          listenerName: item.listenerName || listenerMap.get(item.listenerId)?.name,
          groupName: item.groupName === item.groupId
            ? listenerMap.get(item.listenerId)?.groupName ?? item.groupName
            : item.groupName,
      })));
      const nextActiveTotal = Number(activeResult.value.total ?? activeItems.length);
      const nextPendingTotal = Number(pendingResult.value.total ?? pendingItems.length);
      const nextHistoryTotal = Number(historyResult.value.total ?? historyItems.length);
      const activeLastPage = Math.max(1, Math.ceil(nextActiveTotal / WORK_PAGE_SIZE));
      const pendingLastPage = Math.max(1, Math.ceil(nextPendingTotal / WORK_PAGE_SIZE));
      const historyLastPage = Math.max(1, Math.ceil(nextHistoryTotal / WORK_PAGE_SIZE));
      setActivePage((current) => ({ page: Math.min(current.page, activeLastPage), total: nextActiveTotal }));
      setPendingPage((current) => ({ page: Math.min(current.page, pendingLastPage), total: nextPendingTotal }));
      setHistoryPage((current) => ({ page: Math.min(current.page, historyLastPage), total: nextHistoryTotal }));
      await refreshOpenWorkDetail();
    } catch (error) {
      toast.error("无法读取群监听配置", { description: toUserErrorMessage(error, "请确认后台运行模块已经启动。") });
    } finally {
      if (foregroundSequence === foregroundLoadSequence.current) setLoading(false);
    }
  }, [activePage.page, historyPage.page, pendingPage.page, refreshOpenWorkDetail]);

  const loadEditorOptions = useCallback(async () => {
    const sequence = ++editorOptionsLoadSequence.current;
    const [groupResult, mcpResult, catalogResult] = await Promise.allSettled([
      bridge.listGroups(),
      bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "mcp.list" })),
      bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "mcp.catalog" })),
    ]);
    if (sequence !== editorOptionsLoadSequence.current) return;
    if (groupResult.status === "fulfilled") setGroups(groupResult.value.groups ?? []);
    if (mcpResult.status === "rejected" || catalogResult.status === "rejected") setTools([]);
    if (mcpResult.status !== "fulfilled" || catalogResult.status !== "fulfilled") return;

    const servers = collection<McpServerWithCatalog>(mcpResult.value, ["servers", "items"]);
    const serverMap = new Map(servers.map((server) => [server.id, server]));
    const catalogs = collection<McpCatalogEntry>(catalogResult.value, ["catalogs", "items", "servers", "catalog"])
      .filter((entry) => mcpCatalogAllowsGrants(serverMap.get(entry.serverId), entry))
      .flatMap((entry) => (entry.tools ?? []).map((tool) => ({
          ...tool,
          key: toolGrantSelectionKey(entry.serverId, tool.name, tool.schemaSha256 ?? ""),
          serverId: entry.serverId,
          serverName: serverMap.get(entry.serverId)?.name ?? entry.serverId,
          schemaStatus: tool.schemaStatus ?? "current",
        })));
    setTools(catalogs);
  }, []);

  useEffect(() => {
    const scheduler = createGroupReplyEventRefreshScheduler(
      () => load(true),
      { startPaused: true },
    );
    let disposed = false;
    let unlisten: (() => void) | undefined;
    const handleEvent = (event: ReplyRuntimeEvent) => {
      setListeners((current) => applyGroupReplyRuntimeEvent(current, event));
      scheduler.notify(event);
    };
    void (async () => {
      try {
        const dispose = await bridge.onReplyRuntimeEvent(handleEvent);
        if (disposed) {
          dispose();
          return;
        }
        unlisten = dispose;
      } catch {
        // Runtime queries still provide a useful page if event subscription fails.
      }
      if (disposed) return;
      await load();
      if (!disposed) scheduler.resume();
    })();
    return () => {
      disposed = true;
      scheduler.dispose();
      unlisten?.();
    };
  }, [load]);

  useEffect(() => {
    void loadEditorOptions();
  }, [loadEditorOptions]);

  const openCreate = () => {
    const firstGrantableServerId = tools.find(isMcpToolGrantable)?.serverId;
    const defaultServerToolKeys = firstGrantableServerId
      ? tools.filter((tool) => (
          tool.serverId === firstGrantableServerId
          && isMcpToolGrantable(tool)
        )).map((tool) => tool.key)
      : [];
    setDraft({
      ...listenerDraft(),
      revision: runtimeRevision,
      selectedTools: toggleMcpServerGrant([], defaultServerToolKeys),
    });
    setPersistedGroupId("");
    setExpandedMcpServers([]);
    setAdvancedOpen(false);
    setTestChallenge(null);
    setEditorOpen(true);
  };

  const openEdit = (listener: ReplyListenerSummary) => {
    setDraft({
      id: listener.id,
      revision: runtimeRevision,
      name: listener.name,
      enabled: listener.enabled,
      groupId: listener.groupId,
      groupName: listener.groupName,
      systemPrompt: listener.systemPrompt,
      webhookUrl: "",
      webhookMode: "keep",
      webhookConfigured: listener.webhookConfigured,
      webhookVerified: listener.webhookVerified,
      deliveryMode: listener.deliveryMode,
      selectedTools: listener.tools.map((grant) => toolGrantSelectionKey(grant.serverId, grant.toolName, grant.schemaSha256)),
      tuning: { ...listener.tuning },
    });
    setPersistedGroupId(listener.groupId);
    setExpandedMcpServers([]);
    setAdvancedOpen(false);
    setTestChallenge(null);
    setEditorOpen(true);
  };

  const unavailableToolKeys = useMemo(
    () => draft.selectedTools.filter((key) => !tools.some((candidate) => (
      candidate.key === key && isMcpToolGrantable(candidate)
    ))),
    [draft.selectedTools, tools],
  );

  const toolGroups = useMemo<ToolGroup[]>(() => {
    const grouped = new Map<string, ToolGroup>();
    for (const tool of tools) {
      const group = grouped.get(tool.serverId) ?? {
        serverId: tool.serverId,
        serverName: tool.serverName,
        tools: [],
      };
      group.tools.push(tool);
      grouped.set(tool.serverId, group);
    }
    return [...grouped.values()];
  }, [tools]);

  const selectedToolGrants = useMemo(() => draft.selectedTools.flatMap((key) => {
    const tool = tools.find((candidate) => candidate.key === key);
    return tool && isMcpToolGrantable(tool)
      ? [{ serverId: tool.serverId, toolName: tool.name, schemaSha256: tool.schemaSha256 ?? "" }]
      : [];
  }).filter((grant) => grant.serverId && grant.toolName && grant.schemaSha256), [draft.selectedTools, tools]);

  const saveBlockersFor = (candidate: ListenerDraft) => automaticDeliveryBlockers({
    deliveryMode: candidate.deliveryMode,
    webhookConfigured: candidate.webhookMode === "replace"
      ? Boolean(candidate.webhookUrl.trim())
      : candidate.webhookConfigured,
    webhookVerified: candidate.webhookMode === "keep" && candidate.webhookVerified,
    selectedToolCount: selectedToolGrants.length,
  });

  const saveBlockers = saveBlockersFor(draft);

  const validateDraft = (candidate: ListenerDraft, requireWebhook = false): boolean => {
    if (!candidate.groupId) {
      toast.error("请选择一个群聊");
      return false;
    }
    if (!candidate.name.trim()) {
      toast.error("请填写监听器名称");
      return false;
    }
    if (!selectedToolGrants.length) {
      toast.error("请至少授权一个已发现的 MCP 工具");
      return false;
    }
    const timingErrors = tuningValidationErrors(candidate.tuning);
    if (timingErrors.length) {
      toast.error("时序或并发设置无效", { description: timingErrors[0] });
      return false;
    }
    if (candidate.webhookMode === "replace"
      && !candidate.webhookUrl.trim().startsWith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=")) {
      toast.error("请填写官方企微群机器人 webhook 地址");
      return false;
    }
    const webhookConfigured = candidate.webhookMode === "replace"
      ? Boolean(candidate.webhookUrl.trim())
      : candidate.webhookMode === "keep" && candidate.webhookConfigured;
    if (requireWebhook && !webhookConfigured) {
      toast.error("请先配置企微群机器人 webhook，再发送测试消息");
      return false;
    }
    const blockers = saveBlockersFor(candidate);
    if (blockers.length) {
      toast.error("自动发送尚未通过安全门", { description: blockers[0] });
      return false;
    }
    return true;
  };

  const persistListenerDraft = async (candidate: ListenerDraft): Promise<ListenerDraft> => {
    const webhookPatch = candidate.webhookMode === "replace"
      ? { mode: "replace" as const, value: candidate.webhookUrl.trim() }
      : candidate.webhookMode === "clear" ? { mode: "clear" as const } : { mode: "keep" as const };
    const body = buildListenerSaveBody({
      draft: candidate,
      toolGrants: selectedToolGrants,
      webhookEdit: webhookPatch,
    });
    const rawResult = await executeListenerSave<unknown>({
      draft: candidate,
      body,
      readRevision: async () => {
        const latest = await bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "listener.list" }));
        const revision = latest.revision;
        if (typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 0) {
          throw new Error("后台没有返回有效的配置版本，请刷新后重试。");
        }
        return revision;
      },
      execute: (command) => bridge.replyRuntimeExecute<unknown>(command),
    });
    const result = listenerSaveResultFromWire(rawResult);
    const savedListener = listenerFromWire(result.listener);
    if (!savedListener.id) throw new Error("后台保存成功，但没有返回监听器 ID");
    const nextRevision = result.revision;
    return {
      ...candidate,
      id: savedListener.id,
      revision: nextRevision,
      webhookUrl: "",
      webhookMode: savedListener.webhookConfigured ? "keep" : "replace",
      webhookConfigured: savedListener.webhookConfigured,
      webhookVerified: savedListener.webhookVerified,
      deliveryMode: savedListener.deliveryMode,
    };
  };

  const save = async () => {
    if (!validateDraft(draft)) return;
    setBusy("save");
    try {
      const savedDraft = await persistListenerDraft(draft);
      setRuntimeRevision(savedDraft.revision ?? runtimeRevision);
      setPersistedGroupId(savedDraft.groupId);
      toast.success(draft.id ? "监听器已更新" : "监听器已创建");
      setEditorOpen(false);
      await load(true);
    } catch (error) {
      toast.error("保存失败", { description: toUserErrorMessage(error, "请检查群聊、工具授权和 webhook。") });
    } finally {
      setBusy("");
    }
  };

  const remove = async (listener: ReplyListenerSummary) => {
    if (!confirm(`删除监听器“${listener.name}”？历史记录会保留，但不会再监听新消息。`)) return;
    setBusy(listener.id);
    try {
      await bridge.replyRuntimeExecute(createCommand({ kind: "listener.delete", listenerId: listener.id }, runtimeRevision));
      toast.success("监听器已删除");
      await load(true);
    } catch (error) {
      toast.error("删除失败", { description: toUserErrorMessage(error, "请稍后重试。") });
    } finally {
      setBusy("");
    }
  };

  const testRequiresSave = !draft.id
    || draft.webhookMode !== "keep"
    || draft.groupId !== persistedGroupId;

  const testWebhook = async () => {
    const safeDraft = testRequiresSave && (!draft.webhookVerified || draft.groupId !== persistedGroupId)
      ? { ...draft, deliveryMode: "review" as const }
      : draft;
    if (testRequiresSave) {
      if (!validateDraft(safeDraft, true)) return;
    } else if (!draft.webhookConfigured) {
      return void toast.error("请先配置企微群机器人 webhook，再发送测试消息");
    }
    setBusy("webhook-test");
    let persistedDraft = safeDraft;
    if (testRequiresSave) {
      try {
        persistedDraft = await persistListenerDraft(safeDraft);
        setDraft(persistedDraft);
        setPersistedGroupId(persistedDraft.groupId);
        setRuntimeRevision(persistedDraft.revision ?? runtimeRevision);
      } catch (error) {
        toast.error("保存失败", {
          description: toUserErrorMessage(error, "配置尚未保存，请检查群聊、工具授权和 webhook。"),
        });
        setBusy("");
        return;
      }
    }
    try {
      if (!persistedDraft.id) throw new Error("监听器尚未持久化，无法发送测试消息");
      const testExpectedRevision = testRequiresSave
        ? persistedDraft.revision
        : runtimeRevision;
      const result = await bridge.replyRuntimeExecute<Record<string, unknown>>(createCommand({
        kind: "listener.test_webhook",
        listenerId: persistedDraft.id,
      }, testExpectedRevision));
      const nextRevision = Number(result.revision ?? persistedDraft.revision ?? runtimeRevision);
      setDraft({ ...persistedDraft, revision: nextRevision });
      setTestChallenge({
        listenerId: persistedDraft.id,
        challengeId: String(result.challengeId ?? result.testId ?? ""),
        code: String(result.code ?? result.testCode ?? ""),
        revision: nextRevision,
      });
      setRuntimeRevision(nextRevision);
      toast.success("测试消息已发送", {
        description: testRequiresSave
          ? "配置已安全保存；请到当前选择的群聊确认随机码。"
          : "请到当前选择的群聊确认随机码。",
      });
    } catch (error) {
      toast.error(testRequiresSave ? "配置已保存，但 webhook 测试失败" : "webhook 测试失败", {
        description: toUserErrorMessage(error, "请检查机器人地址和群配置。"),
      });
    } finally {
      setBusy("");
    }
  };

  const confirmWebhook = async () => {
    if (!testChallenge) return;
    setBusy("webhook-confirm");
    try {
      const result = await bridge.replyRuntimeExecute<Record<string, unknown>>(createCommand({
        kind: "listener.confirm_webhook",
        listenerId: testChallenge.listenerId,
        challengeId: testChallenge.challengeId,
        appearedInSelectedGroup: true,
      }, testChallenge.revision));
      const nextRevision = Number(result.revision ?? testChallenge.revision);
      setRuntimeRevision(nextRevision);
      setDraft((current) => ({ ...current, revision: nextRevision, webhookVerified: true, webhookConfigured: true, webhookMode: "keep" }));
      setTestChallenge(null);
      toast.success("群归属已确认", { description: "现在可以选择自动发送。" });
      await load(true);
    } catch (error) {
      toast.error("确认失败", { description: toUserErrorMessage(error, "测试可能已经过期，请重新发送。") });
    } finally {
      setBusy("");
    }
  };

  const workAction = async (item: ReplyWorkItem, kind: WorkActionKind) => {
    const retryingUnknownDelivery = item.stage === "delivery_unknown" && kind !== "work.discard";
    if (retryingUnknownDelivery && !confirm("请先到群里核实：确认上一条消息确实没有出现后再重新发送。即使已核实，网络延迟仍可能造成重复消息。是否继续？")) return;
    if (kind === "work.send_plain_at" && !confirm("普通文本 @姓名 不会触发企微真正提醒。仍然发送吗？")) return;
    if (kind === "work.continue_without_images" && !confirm("缺失的图片不会参与分析，回答可能不完整。确认只使用当前可读的文字和图片继续吗？")) return;
    if (kind === "work.discard" && !confirm(item.stage === "needs_image"
      ? "放弃这条等待图片的任务？此操作不会在群里发送任何消息。"
      : "放弃这条待发送回复？此操作不会在群里发送任何消息。")) return;
    setBusy(`${kind}:${item.id}`);
    try {
      await bridge.replyRuntimeExecute(createCommand(
        buildWorkActionBody(kind, item.id, item.version, retryingUnknownDelivery),
        runtimeRevision,
      ));
      const successCopy: Record<WorkActionKind, string> = {
        "work.send": "回复已提交发送",
        "work.send_plain_at": "回复已提交发送",
        "work.discard": "任务已放弃",
        "work.retry_images": "已重新开始读取图片",
        "work.continue_without_images": "已使用当前可读内容重新分析",
      };
      toast.success(successCopy[kind]);
      closeWorkDetail();
      await load(true);
    } catch (error) {
      toast.error("操作失败", { description: toUserErrorMessage(error, "状态可能已经变化，请刷新后重试。") });
    } finally {
      setBusy("");
    }
  };

  const openWorkDetail = async (item: ReplyWorkItem) => {
    detailRefresher.current?.select(item.id);
    setDetail(item);
    try {
      await detailRefresher.current?.refresh(item.id, () => queryWorkDetail(item.id));
    } catch {
      // The list item remains useful if a detail refresh races with a state transition.
    }
  };

  const activeItems = works.filter((item) => item.status === "waiting" || item.status === "working");
  const pending = works.filter((item) => item.status === "pending");
  const historyItems = works.filter((item) => ["sent", "closed", "failed"].includes(item.status));
  const runtimeAvailability = runtimeAvailabilityCopy(snapshot.running);
  const filteredGroups = groups.filter((group) => {
    const label = String(group.display_name ?? group.name ?? group.id ?? group.conversation_id ?? "");
    return !groupSearch || label.toLowerCase().includes(groupSearch.toLowerCase());
  });

  return (
    <div className="page-content runtime-page reply-page">
      <SectionHeader
        title="群监听回复"
        description="只在识别到问题、等过群友、取得 MCP 证据并独立审核后，才允许生成发送动作。"
        action={(
          <div className="runtime-header-actions">
            <Button
              variant="secondary"
              onClick={() => { void load(); void loadEditorOptions(); }}
              disabled={loading}
            ><RefreshCw size={13} className={loading ? "spin" : undefined} />刷新</Button>
            <Button onClick={openCreate} disabled={loading}><Plus size={14} />新增监听</Button>
          </div>
        )}
      />

      <div className="runtime-command-bar">
        <div className={`runtime-live-indicator ${snapshot.running === true ? "" : "is-stopped"}`}>
          <span className="runtime-live-pulse" />
          <span><strong>{runtimeAvailability.label}</strong><small>{runtimeAvailability.description}</small></span>
        </div>
        <div className="runtime-command-metrics">
          <span><Gauge size={12} />处理中 <strong>{activePage.total}</strong></span>
          <span><Inbox size={12} />待发送 <strong>{snapshot.pendingCount ?? pendingPage.total}</strong></span>
          <span><AlertTriangle size={12} />近期异常 <strong>{snapshot.recentFailures ?? historyItems.filter((item) => item.status === "failed").length}</strong></span>
        </div>
      </div>

      <div className="runtime-tabs" role="tablist">
        <button className={view === "listeners" ? "selected" : ""} onClick={() => setView("listeners")}><Network size={13} />监听配置 <span>{listeners.length}</span></button>
        <button className={view === "active" ? "selected" : ""} onClick={() => setView("active")}><Activity size={13} />处理中 <span>{activePage.total}</span></button>
        <button className={view === "pending" ? "selected" : ""} onClick={() => setView("pending")}><Inbox size={13} />待发送 <span>{pendingPage.total}</span></button>
        <button className={view === "history" ? "selected" : ""} onClick={() => setView("history")}><History size={13} />处理历史 <span>{historyPage.total}</span></button>
      </div>

      {view === "listeners" && (
        loading && !listeners.length ? <RuntimeLoading label="正在读取监听器" />
          : !listeners.length ? (
            <div className="runtime-empty runtime-empty-framed"><MessageCircleQuestion size={30} /><strong>还没有群消息监听</strong><span>首个监听器默认只生成待审核回复，不会自动向群里发送。</span><Button onClick={openCreate}><Plus size={14} />创建监听器</Button></div>
          ) : (
            <div className="listener-grid">
              {listeners.map((listener) => {
                const health = healthCopy(listener.health);
                return (
                  <article className="listener-card" key={listener.id}>
                    <header>
                      <div className="runtime-icon"><UsersRound size={16} /></div>
                      <div><h3>{listener.name}</h3><p>{listener.groupName}</p></div>
                      <span className={`runtime-status runtime-status-${health.tone}`}>{health.label}</span>
                    </header>
                    <div className="listener-flowline" aria-label="处理流程">
                      <span><MessageCircleQuestion size={11} />只认问题</span><ChevronRight size={10} />
                      <span><Clock3 size={11} />补充 {listener.tuning.sameSenderMergeSeconds}s</span><ChevronRight size={10} />
                      <span><Clock3 size={11} />等 {listener.tuning.humanReplyWaitSeconds}s</span><ChevronRight size={10} />
                      <span><Wrench size={11} />MCP 证据</span><ChevronRight size={10} />
                      <span><ShieldCheck size={11} />独立审核</span>
                    </div>
                    <div className="listener-facts">
                      <div><strong>{listener.tools.length}</strong><span>精确授权工具</span></div>
                      <div><strong>{listener.tuning.maxConcurrency}</strong><span>同时检索问题</span></div>
                      <div><strong>{Math.round(listener.tuning.mcpTimeoutSeconds / 60)}m</strong><span>MCP 最长等待</span></div>
                    </div>
                    <div className="listener-delivery">
                      <span className={listener.webhookVerified ? "is-safe" : ""}>{listener.webhookVerified ? <CheckCircle2 size={12} /> : <ShieldAlert size={12} />}{listener.webhookVerified ? "Webhook 群归属已确认" : listener.webhookConfigured ? "Webhook 待群内确认" : "Webhook 未配置"}</span>
                      <span>{listener.deliveryMode === "automatic" ? <><Sparkles size={12} />自动发送</> : <><Eye size={12} />审核后发送</>}</span>
                    </div>
                    {(listener.health === "degraded" || listener.health === "error") && <p className="listener-health-warning"><AlertTriangle size={12} />{listener.healthMessage}</p>}
                    <footer>
                      <span>{listener.enabled ? "监听已启用" : "配置已停用"}</span>
                      <div><button title="编辑" onClick={() => openEdit(listener)}><Edit3 size={14} /></button><button className="danger-icon" title="删除" onClick={() => void remove(listener)}><Trash2 size={14} /></button></div>
                    </footer>
                  </article>
                );
              })}
            </div>
          )
      )}

      {view === "active" && (
        activeItems.length ? <><div className="work-list">{activeItems.map((item) => <WorkCard key={item.id} item={item} onOpen={() => void openWorkDetail(item)} />)}</div><WorkPagination value={activePage} onChange={(page) => setActivePage((current) => ({ ...current, page }))} /></>
          : <div className="runtime-empty runtime-empty-framed"><Activity size={29} /><strong>当前没有处理中问题</strong><span>发现新问题后，可在这里查看补充收集、群友等待和 MCP 检索的实时阶段。</span></div>
      )}

      {view === "pending" && (
        pending.length ? <><div className="work-list">{pending.map((item) => <WorkCard key={item.id} item={item} onOpen={() => void openWorkDetail(item)} />)}</div><WorkPagination value={pendingPage} onChange={(page) => setPendingPage((current) => ({ ...current, page }))} /></>
          : <div className="runtime-empty runtime-empty-framed"><Inbox size={29} /><strong>没有待发送回复</strong><span>审核模式下通过证据检查的回答，会在这里等待发送或放弃。</span></div>
      )}

      {view === "history" && (
        historyItems.length ? <><div className="work-list">{historyItems.map((item) => <WorkCard key={item.id} item={item} onOpen={() => void openWorkDetail(item)} />)}</div><WorkPagination value={historyPage} onChange={(page) => setHistoryPage((current) => ({ ...current, page }))} /></>
          : <div className="runtime-empty runtime-empty-framed"><History size={29} /><strong>暂无处理历史</strong><span>非问题、群友已回答、证据不足、发送成功和失败都会留下可审计结果。</span></div>
      )}

      {editorOpen && (
        <div className="modal-backdrop schedule-modal-backdrop" onMouseDown={(event) => { if (!busy && event.currentTarget === event.target) setEditorOpen(false); }}>
          <section className="schedule-modal runtime-drawer reply-drawer" role="dialog" aria-modal="true" aria-busy={Boolean(busy)} aria-label={draft.id ? "编辑监听器" : "新增监听器"}>
            <header className="schedule-modal-header"><div><span className="drawer-eyebrow">QUESTION REPLY POLICY</span><h2>{draft.id ? "编辑群监听" : "新增群监听"}</h2></div><button aria-label="关闭" disabled={Boolean(busy)} onClick={() => setEditorOpen(false)}><X size={16} /></button></header>
            <div className="schedule-modal-body runtime-drawer-body">
              <fieldset className="runtime-drawer-fields" disabled={Boolean(busy)}>
              <section className="runtime-form-section">
                <div className="runtime-form-heading"><UsersRound size={14} /><span><strong>监听对象</strong><small>一个监听器只能选择一个群聊；同一个群只能启用一个监听器。</small></span></div>
                <Field label="配置名称"><Input value={draft.name} placeholder={draft.groupName ? `${draft.groupName}自动答疑` : "例如：售后群自动答疑"} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></Field>
                <div className="single-group-picker">
                  <div className="single-group-search"><Search size={13} /><input value={groupSearch} placeholder="搜索群聊" onChange={(event) => setGroupSearch(event.target.value)} /><span>只能选择一个群聊</span></div>
                  <div className="single-group-list">
                    {filteredGroups.map((group) => {
                      const id = String(group.id ?? group.conversation_id ?? "");
                      const name = String(group.name ?? group.display_name ?? id);
                      return <button type="button" className={draft.groupId === id ? "selected" : ""} key={id} onClick={() => {
                        const sameGroup = draft.groupId === id;
                        setDraft({
                          ...draft,
                          groupId: id,
                          groupName: name,
                          name: draft.name || `${name}自动答疑`,
                          webhookVerified: sameGroup && draft.webhookVerified,
                          deliveryMode: sameGroup ? draft.deliveryMode : "review",
                        });
                        setTestChallenge(null);
                      }}><span className="radio-dot">{draft.groupId === id && <Check size={10} />}</span><span><strong>{name}</strong><small>{id}</small></span></button>;
                    })}
                  </div>
                </div>
              </section>

              <section className="runtime-form-section">
                <div className="runtime-form-heading"><Wrench size={14} /><span><strong>精确 MCP 工具授权</strong><small>按服务折叠；新建时默认选择第一个可用服务的全部工具，展开后可逐项调整。</small></span></div>
                <div className="tool-grant-list">
                  {unavailableToolKeys.map((key) => {
                    const grant = toolGrantFromSelectionKey(key);
                    return <button type="button" key={key} className="invalid-grant" onClick={() => setDraft({ ...draft, selectedTools: draft.selectedTools.filter((item) => item !== key) })}><span className="grant-check"><XCircle size={10} /></span><span><strong>{grant?.toolName || "未知工具"}</strong><small>{grant?.serverId || "未知 MCP 服务"} · 原授权已失效</small></span><em className="schema-changed">点击移除</em></button>;
                  })}
                  {toolGroups.length ? toolGroups.map((group) => {
                    const grantableKeys = group.tools.filter(isMcpToolGrantable).map((tool) => tool.key);
                    const grantState = mcpServerGrantState(draft.selectedTools, grantableKeys);
                    const selectedCount = grantableKeys.filter((key) => draft.selectedTools.includes(key)).length;
                    const expanded = expandedMcpServers.includes(group.serverId);
                    return <section className={`mcp-grant-group state-${grantState}`} key={group.serverId}>
                      <div className="mcp-grant-server-row">
                        <button
                          type="button"
                          className={`mcp-server-select ${grantState}`}
                          disabled={!grantableKeys.length}
                          role="checkbox"
                          aria-checked={grantState === "partial" ? "mixed" : grantState === "all"}
                          onClick={() => setDraft({
                            ...draft,
                            selectedTools: toggleMcpServerGrant(draft.selectedTools, grantableKeys),
                          })}
                        >
                          <span className="grant-check">{grantState === "all" ? <Check size={10} /> : grantState === "partial" ? <Minus size={10} /> : null}</span>
                          <span><strong>{group.serverName}</strong><small>{grantableKeys.length ? `${selectedCount}/${grantableKeys.length} 个工具已授权` : "没有 Schema 有效的可授权工具"}</small></span>
                          <em>{grantState === "all" ? "全部" : grantState === "partial" ? "部分" : "未选择"}</em>
                        </button>
                        <button
                          type="button"
                          className="mcp-server-expand"
                          aria-label={`${expanded ? "收起" : "展开"}${group.serverName}工具`}
                          aria-expanded={expanded}
                          onClick={() => setExpandedMcpServers((current) => (
                            current.includes(group.serverId)
                              ? current.filter((id) => id !== group.serverId)
                              : [...current, group.serverId]
                          ))}
                        ><ChevronDown size={14} className={expanded ? "is-open" : ""} /></button>
                      </div>
                      {expanded && <div className="mcp-grant-tools">
                        {group.tools.map((tool) => {
                          const selected = draft.selectedTools.includes(tool.key);
                          const grantable = isMcpToolGrantable(tool);
                          const unavailableLabel = tool.schemaStatus === "changed" ? "Schema 已变化" : "Schema 未确认";
                          return <button type="button" role="checkbox" aria-checked={selected} key={tool.key} disabled={!grantable} className={`mcp-grant-tool ${selected ? "selected" : ""}`} onClick={() => setDraft({ ...draft, selectedTools: toggleToolSelection(draft.selectedTools, tool.key) })}><span className="grant-check">{selected && <Check size={10} />}</span><span><strong>{tool.title || tool.name}</strong><small>{tool.description || "未提供说明"}</small></span><em className={!grantable ? "schema-changed" : ""}>{grantable ? `Schema ${tool.schemaSha256?.slice(0, 8)}` : unavailableLabel}</em></button>;
                        })}
                      </div>}
                    </section>;
                  }) : <div className="runtime-inline-empty"><CircleOff size={15} />请先在 MCP 服务页面连接并发现工具。</div>}
                </div>
              </section>

              <section className="runtime-form-section">
                <div className="runtime-form-heading"><Bot size={14} /><span><strong>回答规则</strong><small>这里只控制回答范围和措辞，不能覆盖证据门槛和安全规则。</small></span></div>
                <Field label="系统提示词"><textarea className="input runtime-textarea" value={draft.systemPrompt} onChange={(event) => setDraft({ ...draft, systemPrompt: event.target.value })} /></Field>
              </section>

              <section className="runtime-form-section">
                <div className="runtime-form-heading"><Webhook size={14} /><span><strong>企微群机器人</strong><small>只用于消息推送。自动发送前必须完成一次群内可见测试。</small></span></div>
                {draft.id && draft.webhookConfigured && (
                  <div className="runtime-segmented webhook-secret-mode"><button type="button" className={draft.webhookMode === "keep" ? "selected" : ""} onClick={() => setDraft({ ...draft, webhookMode: "keep", webhookUrl: "" })}>保留</button><button type="button" className={draft.webhookMode === "replace" ? "selected" : ""} onClick={() => { setDraft({ ...draft, webhookMode: "replace", webhookVerified: false, deliveryMode: "review" }); setTestChallenge(null); }}>替换</button><button type="button" className={draft.webhookMode === "clear" ? "selected danger" : ""} onClick={() => { setDraft({ ...draft, webhookMode: "clear", webhookUrl: "", webhookVerified: false, deliveryMode: "review" }); setTestChallenge(null); }}>清空</button></div>
                )}
                {(draft.webhookMode === "replace" || !draft.id) && <Field label="Webhook URL" hint="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"><Input type="password" value={draft.webhookUrl} placeholder="粘贴机器人 webhook" onChange={(event) => setDraft({ ...draft, webhookUrl: event.target.value, webhookConfigured: Boolean(event.target.value), webhookVerified: false })} /></Field>}
                <div className="webhook-verification-row">
                  <span className={draft.webhookVerified ? "verified" : ""}>{draft.webhookVerified ? <CheckCircle2 size={14} /> : <ShieldAlert size={14} />}<span><strong>{draft.webhookVerified ? "已确认机器人属于所选群" : "尚未完成群归属确认"}</strong><small>HTTP 成功不能证明机器人发到了正确的群。</small></span></span>
                  <Button variant="secondary" disabled={busy === "webhook-test" || busy === "save" || busy === "webhook-confirm"} onClick={() => void testWebhook()}>{busy === "webhook-test" ? <LoaderCircle className="spin" size={12} /> : <Send size={12} />}{testRequiresSave ? "保存并发送测试" : "发送测试"}</Button>
                </div>
                {testChallenge && <div className="webhook-challenge"><span><strong>请在“{draft.groupName}”确认测试消息</strong><small>{testChallenge.code ? `随机码：${testChallenge.code}` : "确认看到刚才的测试消息后再继续。"}</small></span><Button onClick={() => void confirmWebhook()} disabled={busy === "webhook-confirm"}>我已在该群看到</Button></div>}
                <div className="delivery-mode-grid">
                  <button type="button" className={draft.deliveryMode === "review" ? "selected" : ""} onClick={() => setDraft({ ...draft, deliveryMode: "review" })}><Eye size={16} /><span><strong>审核后发送</strong><small>默认安全模式，可发送或放弃。</small></span></button>
                  <button type="button" className={draft.deliveryMode === "automatic" ? "selected" : ""} onClick={() => {
                    if (!draft.webhookVerified) return void toast.warning("自动发送需要先完成 webhook 群内可见确认");
                    setDraft({ ...draft, deliveryMode: "automatic" });
                  }}><Sparkles size={16} /><span><strong>自动发送</strong><small>真艾特不可解析时仍转入待发送。</small></span></button>
                </div>
              </section>

              <section className="runtime-form-section advanced-settings">
                <button type="button" className="advanced-toggle" onClick={() => setAdvancedOpen((value) => !value)}><span><Gauge size={14} /><span><strong>时序与并发</strong><small>默认值适合多数工作群和最长约 15 分钟的 MCP。</small></span></span><ChevronDown size={14} className={advancedOpen ? "is-open" : ""} /></button>
                {advancedOpen && <div className="advanced-grid">
                  <NumberField label="监听刷新间隔" suffix="秒" value={draft.tuning.pollIntervalSeconds} min={2} max={60} hint="只决定后台多久检查一次企微本地新消息；不是从发送到开始 MCP 检索的总耗时。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, pollIntervalSeconds: value } })} />
                  <NumberField label="等待连续补充时长" suffix="秒" value={draft.tuning.sameSenderMergeSeconds} min={2} max={120} hint="仅在收集期内生效；同一人的有效补充会从最后一条重新计时。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, sameSenderMergeSeconds: value } })} />
                  <NumberField label="留给群友回答的时间" suffix="秒" value={draft.tuning.humanReplyWaitSeconds} min={10} max={3600} hint="补充收集结束后开始计时。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, humanReplyWaitSeconds: value } })} />
                  <NumberField label="个人上下文保留时间" suffix="分钟" value={Math.round(draft.tuning.sessionTimeoutSeconds / 60)} min={1} max={1440} hint="只保留同一人在同一群的历史。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, sessionTimeoutSeconds: value * 60 } })} />
                  <NumberField label="同时检索问题数" suffix="个" value={draft.tuning.maxConcurrency} min={1} max={20} hint="同一个人始终串行。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, maxConcurrency: value } })} />
                  <NumberField label="单个问题 MCP 最长等待" suffix="秒" value={draft.tuning.mcpTimeoutSeconds} min={60} max={1800} hint="默认 900 秒；证据不足或超时都不发送。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, mcpTimeoutSeconds: value } })} />
                </div>}
              </section>

              <Switch checked={draft.enabled} onChange={(enabled) => setDraft({ ...draft, enabled })} label="启用群监听" description="保存后立即应用；应用退出期间的消息不会补处理。" />
              {saveBlockers.length > 0 && draft.deliveryMode === "automatic" && <div className="runtime-safety-note is-danger"><ShieldAlert size={14} /><span><strong>自动发送尚未通过安全门</strong>{saveBlockers.join(" ")}</span></div>}
              </fieldset>
            </div>
            <footer className="schedule-modal-footer"><Button variant="secondary" onClick={() => setEditorOpen(false)} disabled={Boolean(busy)}>取消</Button><Button onClick={() => void save()} disabled={Boolean(busy)}>{busy === "save" ? <LoaderCircle className="spin" size={13} /> : <Save size={13} />}保存监听器</Button></footer>
          </section>
        </div>
      )}

      {detail && (
        <div className="modal-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) closeWorkDetail(); }}>
          <section className="modal-card work-detail-modal" role="dialog" aria-modal="true">
            <button className="work-detail-close" aria-label="关闭" onClick={closeWorkDetail}><X size={15} /></button>
            <div className="work-detail-kicker"><MessageSquareReply size={14} />{detail.groupName} · {detail.senderName}</div>
            <h2>{detail.stage === "needs_image" ? "需要图片" : detail.status === "pending" ? "待发送回复" : "处理详情"}</h2>
            {(detail.imageCount ?? 0) > 0 && <div className={`work-outcome-banner is-${detail.imageStatus ?? "none"}`}>
              {detail.stage === "needs_image" || ["partial", "unavailable", "unsupported"].includes(detail.imageStatus ?? "")
                ? <AlertTriangle size={18} />
                : detail.imageStatus === "resolving" ? <LoaderCircle className="spin" size={18} /> : <CheckCircle2 size={18} />}
              <span>
                <strong>{detail.stage === "needs_image" ? "图片未参与分析，系统已停止自动回复" : "图片处理结果"}</strong>
                <small>{imageStatusCopy(detail.imageStatus, detail.imageCount, detail.imageAvailableCount, detail.imageUnavailableCount)}</small>
              </span>
            </div>}
            <div className="work-stage-timeline" aria-label="处理阶段">
              {workStageTimeline(detail).map((step) => (
                <div className={`work-stage-step is-${step.state}`} key={step.key}>
                  <span className="work-stage-marker">
                    {step.state === "complete" ? <Check size={11} />
                      : step.state === "current" ? <LoaderCircle className="spin" size={11} />
                        : step.state === "failed" ? <X size={11} />
                        : step.state === "skipped" ? <CircleOff size={11} /> : <span />}
                  </span>
                  <span><strong>{step.label}</strong><small>{workStageStepCopy(step)}</small></span>
                </div>
              ))}
            </div>
            <div className="work-detail-context">
              {detail.sourceDelaySeconds !== undefined && <div className="image-state"><Clock3 size={13} /><span><strong>来源观测延迟</strong><small>消息发送后约 {detail.sourceDelaySeconds.toFixed(1)} 秒被本地监听发现（含企微写库与轮询）</small></span></div>}
              <div className={`image-state is-${detail.imageStatus ?? "none"}`}><Eye size={13} /><span><strong>图片读取</strong><small>{imageStatusCopy(detail.imageStatus, detail.imageCount, detail.imageAvailableCount, detail.imageUnavailableCount)}</small></span></div>
              {(detail.duplicateCount ?? 0) > 0 && <div className="duplicate-state"><History size={13} /><span><strong>重复记录已合并</strong><small>已折叠 {detail.duplicateCount} 条重复记录</small></span></div>}
            </div>
            <div className="work-detail-section"><span>识别到的问题</span><p>{detail.question || "问题内容不可用"}</p></div>
            <div className="work-detail-section answer"><span>基于 MCP 证据的回答</span><p>{workAnswerCopy(detail)}</p></div>
            {detail.evidence?.length ? <details className="work-evidence"><summary><Wrench size={14} />检索证据（{detail.evidence.length}）<ChevronDown size={14} /></summary>{detail.evidence.map((entry, index) => <div key={`${entry.toolName}-${index}`}><Wrench size={13} /><span><strong>{entry.serverName || "MCP"} / {entry.toolName || "工具"}</strong><small>{entry.summary}</small></span></div>)}</details> : null}
            {detail.stage === "delivery_unknown" && <div className="runtime-safety-note is-danger"><ShieldAlert size={14} /><span><strong>发送结果未知</strong>系统不会自动重发。请先到群里核实；只有确认消息确实未出现后，才能明确选择重新发送。</span></div>}
            {detail.stage === "needs_image" ? <div className="work-detail-actions is-image-actions">
              <Button onClick={() => void workAction(detail, "work.retry_images")} disabled={busy.includes(detail.id)}><RefreshCw size={14} />重新读取图片</Button>
              <Button variant="secondary" onClick={() => void workAction(detail, "work.continue_without_images")} disabled={busy.includes(detail.id)}><Eye size={14} />使用现有内容继续</Button>
              <Button variant="danger" onClick={() => void workAction(detail, "work.discard")} disabled={busy.includes(detail.id)}><XCircle size={14} />放弃任务</Button>
            </div> : detail.status === "pending" && <div className="work-detail-actions">
              {detail.stage === "delivery_unknown"
                ? (detail.mentionMode !== "unresolved"
                    ? <Button variant="secondary" onClick={() => void workAction(detail, "work.send")} disabled={busy.includes(detail.id)}><Send size={13} />确认群内未出现，重新发送</Button>
                    : <Button variant="secondary" onClick={() => void workAction(detail, "work.send_plain_at")} disabled={busy.includes(detail.id)}><AlertTriangle size={13} />确认未出现，普通 @ 重发</Button>)
                : (detail.mentionMode !== "unresolved" ? <Button onClick={() => void workAction(detail, "work.send")} disabled={busy.includes(detail.id)}><Send size={13} />发送并真艾特</Button> : <Button variant="secondary" onClick={() => void workAction(detail, "work.send_plain_at")} disabled={busy.includes(detail.id)}><AlertTriangle size={13} />普通 @姓名</Button>)}
              <Button variant="danger" onClick={() => void workAction(detail, "work.discard")} disabled={busy.includes(detail.id)}><XCircle size={13} />放弃发送</Button>
            </div>}
          </section>
        </div>
      )}
    </div>
  );
}

function RuntimeLoading({ label }: { label: string }) {
  return <div className="runtime-empty"><LoaderCircle className="spin" /><strong>{label}</strong><span>正在同步本机运行状态。</span></div>;
}

function WorkPagination({ value, onChange }: { value: WorkPageState; onChange: (page: number) => void }) {
  const totalPages = Math.max(1, Math.ceil(value.total / WORK_PAGE_SIZE));
  if (!value.total) return null;
  return (
    <nav className="work-pagination" aria-label="回复记录分页">
      <span>第 <strong>{value.page}</strong> / {totalPages} 页 · 共 {value.total} 条</span>
      <div>
        <button type="button" disabled={value.page <= 1} onClick={() => onChange(value.page - 1)}>上一页</button>
        <button type="button" disabled={value.page >= totalPages} onClick={() => onChange(value.page + 1)}>下一页</button>
      </div>
    </nav>
  );
}

function WorkCard({ item, onOpen }: { item: ReplyWorkItem; onOpen: () => void }) {
  const status = workStatusCopy(item);
  return <button className="work-card" onClick={onOpen}><div className={`work-card-status status-${item.status}`}>{item.status === "pending" ? <Inbox size={15} /> : item.status === "sent" ? <CheckCircle2 size={15} /> : item.status === "failed" ? <XCircle size={15} /> : <Activity size={15} />}</div><div className="work-card-main"><div><strong>{item.senderName}</strong><span>{item.groupName}</span>{(item.duplicateCount ?? 0) > 0 && <em className="work-duplicate-badge"><History size={10} />已折叠 {item.duplicateCount}</em>}</div><p>{item.question || "问题内容不可用"}</p><small>{workAnswerCopy(item)}</small></div><div className="work-card-side"><span>{status}</span><time>{dateLabel(item.updatedAt || item.createdAt)}</time><ChevronRight size={13} /></div></button>;
}

function NumberField({ label, suffix, value, min, max, hint, onChange }: { label: string; suffix: string; value: number; min: number; max: number; hint: string; onChange: (value: number) => void }) {
  return <Field label={label} hint={`${hint} 范围 ${min}–${max}${suffix}。`}><div className="number-field"><Input type="number" min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} /><span>{suffix}</span></div></Field>;
}

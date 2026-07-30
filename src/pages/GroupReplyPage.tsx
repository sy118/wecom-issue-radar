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
  automaticDeliveryBlockers,
  buildListenerSaveBody,
  buildWorkActionBody,
  createCommand,
  createQuery,
  defaultListenerDraft,
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
} from "../lib/replyRuntimeUi";
import type {
  GroupInfo,
  ListenerToolGrant,
  McpServerSummary,
  McpToolSummary,
  ReplyListenerSummary,
  ReplyRuntimeSnapshot,
  ReplyWorkItem,
  ReplyWorkStatus,
} from "../types";

type ViewMode = "listeners" | "pending" | "history";

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
  server_unhealthy: "MCP 服务连接测试失败，请先修复服务并重新测试连接。",
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
    healthMessage: listenerHealthMessages[healthStatus]
      ?? String(health.message ?? healthStatus),
    pendingCount: Number(raw.pendingCount ?? 0),
    lastPollAt: String(raw.lastPollAt ?? ""),
  };
}

function normalizeWorkStatus(value: string): ReplyWorkStatus {
  if (["collecting", "classifying", "waiting_for_human_reply", "queued_retrieval"].includes(value)) return "waiting";
  if (["retrieving", "reviewing", "sending", "queued_delivery"].includes(value)) return "working";
  if (["pending", "awaiting_review", "delivery_unknown", "delivery_failed"].includes(value)) return "pending";
  if (value === "sent") return "sent";
  if (["failed", "mcp_timeout"].includes(value)) return "failed";
  return "closed";
}

function workFromWire(raw: Record<string, unknown>): ReplyWorkItem {
  const identity = (raw.identity ?? {}) as Record<string, unknown>;
  const mention = (raw.mention ?? {}) as Record<string, unknown>;
  const rawError = (raw.error ?? {}) as Record<string, unknown>;
  const question = raw.question;
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
    mentionMode: (raw.mentionMode ?? identity.mentionMode
      ?? (mention.accountConfigured ? "userid" : mention.mobileConfigured ? "mobile" : "unresolved")) as ReplyWorkItem["mentionMode"],
    createdAt: String(raw.createdAt ?? raw.created_at ?? ""),
    updatedAt: String(raw.updatedAt ?? raw.updated_at ?? ""),
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

export function GroupReplyPage() {
  const [view, setView] = useState<ViewMode>("listeners");
  const [listeners, setListeners] = useState<ReplyListenerSummary[]>([]);
  const [works, setWorks] = useState<ReplyWorkItem[]>([]);
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
  const eventRefreshTimer = useRef<number>();
  const loadSequence = useRef(0);

  const load = useCallback(async (quiet = false) => {
    const sequence = ++loadSequence.current;
    if (!quiet) setLoading(true);
    try {
      const [listenerResult, pendingResult, historyResult, snapshotResult, groupResult, mcpResult, catalogResult] = await Promise.allSettled([
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "listener.list" })),
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
        bridge.listGroups(),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "mcp.list" })),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "mcp.catalog" })),
      ]);
      if (sequence !== loadSequence.current) return;
      if (snapshotResult.status === "fulfilled") setSnapshot(snapshotFromWire(snapshotResult.value));
      else setSnapshot({});
      if (mcpResult.status === "rejected" || catalogResult.status === "rejected") setTools([]);
      if (listenerResult.status === "rejected") throw listenerResult.reason;
      if (pendingResult.status === "rejected") throw pendingResult.reason;
      if (historyResult.status === "rejected") throw historyResult.reason;
      const rawListeners = collection<Record<string, unknown>>(listenerResult.value, ["listeners", "items"]);
      const nextListeners = rawListeners.map(listenerFromWire);
      setListeners(nextListeners);
      setRuntimeRevision(Number(listenerResult.value.revision ?? 0));
      const listenerMap = new Map(nextListeners.map((listener) => [listener.id, listener]));
      const pendingItems = collection<Record<string, unknown>>(pendingResult.value, ["items", "works"]);
      const historyItems = collection<Record<string, unknown>>(historyResult.value, ["items", "works"]);
      const workItems = [...pendingItems, ...historyItems];
      setWorks(workItems.map(workFromWire).map((item) => ({
          ...item,
          listenerName: item.listenerName || listenerMap.get(item.listenerId)?.name,
          groupName: item.groupName === item.groupId
            ? listenerMap.get(item.listenerId)?.groupName ?? item.groupName
            : item.groupName,
      })));
      const nextPendingTotal = Number(pendingResult.value.total ?? pendingItems.length);
      const nextHistoryTotal = Number(historyResult.value.total ?? historyItems.length);
      const pendingLastPage = Math.max(1, Math.ceil(nextPendingTotal / WORK_PAGE_SIZE));
      const historyLastPage = Math.max(1, Math.ceil(nextHistoryTotal / WORK_PAGE_SIZE));
      setPendingPage((current) => ({ page: Math.min(current.page, pendingLastPage), total: nextPendingTotal }));
      setHistoryPage((current) => ({ page: Math.min(current.page, historyLastPage), total: nextHistoryTotal }));
      if (groupResult.status === "fulfilled") setGroups(groupResult.value.groups ?? []);

      if (mcpResult.status === "fulfilled" && catalogResult.status === "fulfilled") {
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
      }
    } catch (error) {
      toast.error("无法读取群监听配置", { description: toUserErrorMessage(error, "请确认后台运行模块已经启动。") });
    } finally {
      if (sequence === loadSequence.current) setLoading(false);
    }
  }, [historyPage.page, pendingPage.page]);

  useEffect(() => {
    void load();
    let unlisten: (() => void) | undefined;
    void bridge.onReplyRuntimeEvent(() => {
      window.clearTimeout(eventRefreshTimer.current);
      eventRefreshTimer.current = window.setTimeout(() => void load(true), 180);
    }).then((dispose) => { unlisten = dispose; });
    return () => {
      window.clearTimeout(eventRefreshTimer.current);
      unlisten?.();
    };
  }, [load]);

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
    const result = await bridge.replyRuntimeExecute<Record<string, unknown>>(createCommand(buildListenerSaveBody({
      draft: candidate,
      toolGrants: selectedToolGrants,
      webhookEdit: webhookPatch,
    }), candidate.revision));
    if (!result.listener || typeof result.listener !== "object" || Array.isArray(result.listener)) {
      throw new Error("后台保存成功，但没有返回监听器状态");
    }
    const savedListener = listenerFromWire(result.listener as Record<string, unknown>);
    if (!savedListener.id) throw new Error("后台保存成功，但没有返回监听器 ID");
    const nextRevision = Number(result.revision ?? savedListener.revision ?? candidate.revision ?? runtimeRevision);
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
    let savedBeforeTest = false;
    try {
      const persistedDraft = testRequiresSave
        ? await persistListenerDraft(safeDraft)
        : safeDraft;
      if (!persistedDraft.id) throw new Error("监听器尚未持久化，无法发送测试消息");
      if (testRequiresSave) {
        savedBeforeTest = true;
        setDraft(persistedDraft);
        setPersistedGroupId(persistedDraft.groupId);
        setRuntimeRevision(persistedDraft.revision ?? runtimeRevision);
      }
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
      toast.error(savedBeforeTest ? "配置已保存，但 webhook 测试失败" : "webhook 测试失败", {
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

  const workAction = async (item: ReplyWorkItem, kind: "work.send" | "work.send_plain_at" | "work.discard") => {
    const retryingUnknownDelivery = item.stage === "delivery_unknown" && kind !== "work.discard";
    if (retryingUnknownDelivery && !confirm("请先到群里核实：确认上一条消息确实没有出现后再重新发送。即使已核实，网络延迟仍可能造成重复消息。是否继续？")) return;
    if (kind === "work.send_plain_at" && !confirm("普通文本 @姓名 不会触发企微真正提醒。仍然发送吗？")) return;
    if (kind === "work.discard" && !confirm("放弃这条待发送回复？此操作不会在群里发送任何消息。")) return;
    setBusy(`${kind}:${item.id}`);
    try {
      await bridge.replyRuntimeExecute(createCommand(
        buildWorkActionBody(kind, item.id, item.version, retryingUnknownDelivery),
        runtimeRevision,
      ));
      toast.success(kind === "work.discard" ? "已放弃发送" : "回复已提交发送");
      setDetail(null);
      await load(true);
    } catch (error) {
      toast.error("操作失败", { description: toUserErrorMessage(error, "状态可能已经变化，请刷新后重试。") });
    } finally {
      setBusy("");
    }
  };

  const openWorkDetail = async (item: ReplyWorkItem) => {
    setDetail(item);
    try {
      const result = await bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "work.detail", workId: item.id }));
      const raw = (result.item ?? result.work ?? result) as Record<string, unknown>;
      setDetail(workFromWire(raw));
    } catch {
      // The list item remains useful if a detail refresh races with a state transition.
    }
  };

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
            <Button variant="secondary" onClick={() => void load()} disabled={loading}><RefreshCw size={13} className={loading ? "spin" : undefined} />刷新</Button>
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
          <span><Gauge size={12} />检索中 <strong>{snapshot.activeRetrievals ?? works.filter((item) => item.status === "working").length}</strong></span>
          <span><Inbox size={12} />待发送 <strong>{snapshot.pendingCount ?? pendingPage.total}</strong></span>
          <span><AlertTriangle size={12} />近期异常 <strong>{snapshot.recentFailures ?? historyItems.filter((item) => item.status === "failed").length}</strong></span>
        </div>
      </div>

      <div className="runtime-tabs" role="tablist">
        <button className={view === "listeners" ? "selected" : ""} onClick={() => setView("listeners")}><Network size={13} />监听配置 <span>{listeners.length}</span></button>
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
                    {listener.health === "degraded" && <p className="listener-health-warning"><AlertTriangle size={12} />{listener.healthMessage}</p>}
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
            <fieldset className="schedule-modal-body runtime-drawer-body" disabled={Boolean(busy)}>
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
                  <NumberField label="监听刷新间隔" suffix="秒" value={draft.tuning.pollIntervalSeconds} min={2} max={60} hint="只决定多久检查一次新消息。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, pollIntervalSeconds: value } })} />
                  <NumberField label="同一人的连续补充合并间隔" suffix="秒" value={draft.tuning.sameSenderMergeSeconds} min={2} max={120} hint="默认 20 秒；不是历史会话时间。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, sameSenderMergeSeconds: value } })} />
                  <NumberField label="留给群友回答的时间" suffix="秒" value={draft.tuning.humanReplyWaitSeconds} min={10} max={3600} hint="提问者补充后重新计时。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, humanReplyWaitSeconds: value } })} />
                  <NumberField label="个人上下文保留时间" suffix="分钟" value={Math.round(draft.tuning.sessionTimeoutSeconds / 60)} min={1} max={1440} hint="只保留同一人在同一群的历史。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, sessionTimeoutSeconds: value * 60 } })} />
                  <NumberField label="同时检索问题数" suffix="个" value={draft.tuning.maxConcurrency} min={1} max={20} hint="同一个人始终串行。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, maxConcurrency: value } })} />
                  <NumberField label="单个问题 MCP 最长等待" suffix="秒" value={draft.tuning.mcpTimeoutSeconds} min={60} max={1800} hint="默认 900 秒；证据不足或超时都不发送。" onChange={(value) => setDraft({ ...draft, tuning: { ...draft.tuning, mcpTimeoutSeconds: value } })} />
                </div>}
              </section>

              <Switch checked={draft.enabled} onChange={(enabled) => setDraft({ ...draft, enabled })} label="启用群监听" description="保存后立即应用；应用退出期间的消息不会补处理。" />
              {saveBlockers.length > 0 && draft.deliveryMode === "automatic" && <div className="runtime-safety-note is-danger"><ShieldAlert size={14} /><span><strong>自动发送尚未通过安全门</strong>{saveBlockers.join(" ")}</span></div>}
            </fieldset>
            <footer className="schedule-modal-footer"><Button variant="secondary" onClick={() => setEditorOpen(false)} disabled={Boolean(busy)}>取消</Button><Button onClick={() => void save()} disabled={Boolean(busy)}>{busy === "save" ? <LoaderCircle className="spin" size={13} /> : <Save size={13} />}保存监听器</Button></footer>
          </section>
        </div>
      )}

      {detail && (
        <div className="modal-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) setDetail(null); }}>
          <section className="modal-card work-detail-modal" role="dialog" aria-modal="true">
            <button className="work-detail-close" aria-label="关闭" onClick={() => setDetail(null)}><X size={15} /></button>
            <div className="work-detail-kicker"><MessageSquareReply size={14} />{detail.groupName} · {detail.senderName}</div>
            <h2>{detail.status === "pending" ? "待发送回复" : "处理详情"}</h2>
            <div className="work-detail-section"><span>识别到的问题</span><p>{detail.question || "问题内容不可用"}</p></div>
            <div className="work-detail-section answer"><span>基于 MCP 证据的回答</span><p>{detail.answer || "回答仍在生成中"}</p></div>
            {detail.evidence?.length ? <div className="work-evidence"><span>检索证据</span>{detail.evidence.map((entry, index) => <div key={`${entry.toolName}-${index}`}><Wrench size={11} /><span><strong>{entry.serverName || "MCP"} / {entry.toolName || "工具"}</strong><small>{entry.summary}</small></span></div>)}</div> : null}
            {detail.stage === "delivery_unknown" && <div className="runtime-safety-note is-danger"><ShieldAlert size={14} /><span><strong>发送结果未知</strong>系统不会自动重发。请先到群里核实；只有确认消息确实未出现后，才能明确选择重新发送。</span></div>}
            {detail.status === "pending" && <div className="work-detail-actions">
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
  const status = item.status === "pending" ? "待发送" : item.status === "sent" ? "已发送" : item.status === "failed" ? "失败" : item.status === "closed" ? "已结束" : item.stage || "处理中";
  return <button className="work-card" onClick={onOpen}><div className={`work-card-status status-${item.status}`}>{item.status === "pending" ? <Inbox size={15} /> : item.status === "sent" ? <CheckCircle2 size={15} /> : item.status === "failed" ? <XCircle size={15} /> : <Activity size={15} />}</div><div className="work-card-main"><div><strong>{item.senderName}</strong><span>{item.groupName}</span></div><p>{item.question || "问题内容不可用"}</p><small>{item.answer || item.reason || "等待处理结果"}</small></div><div className="work-card-side"><span>{status}</span><time>{dateLabel(item.updatedAt || item.createdAt)}</time><ChevronRight size={13} /></div></button>;
}

function NumberField({ label, suffix, value, min, max, hint, onChange }: { label: string; suffix: string; value: number; min: number; max: number; hint: string; onChange: (value: number) => void }) {
  return <Field label={label} hint={`${hint} 范围 ${min}–${max}${suffix}。`}><div className="number-field"><Input type="number" min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} /><span>{suffix}</span></div></Field>;
}

import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  CalendarClock,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleMinus,
  CloudUpload,
  Clock3,
  Edit3,
  FolderOpen,
  History,
  Info,
  LoaderCircle,
  Play,
  Plus,
  Power,
  RefreshCw,
  Table2,
  Trash2,
  UsersRound,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import { smartSheetConfigurationBlockers } from "../lib/smartSheetSync";
import type {
  AppConfig,
  GroupInfo,
  PendingScheduleSync,
  ProcessingOptions,
  ScheduleDefinition,
  ScheduleExecutionHistoryItem,
  ScheduleExecutionHistoryPage,
  ScheduleExecutionStatus,
  ScheduleEvent,
  ScheduleDateMode,
  TaskGroup,
  TaskResult,
  TaskRunResult,
} from "../types";
import { Button, Field, Input, SectionHeader, Switch } from "../components/ui";
import {
  defaultProcessingOptions,
  GroupMultiSelect,
  ProcessingOptionsEditor,
  resolveSmartSheetTemplateId,
} from "../components/TaskEditor";

const weekdays = [
  { value: 1, label: "一" },
  { value: 2, label: "二" },
  { value: 3, label: "三" },
  { value: 4, label: "四" },
  { value: 5, label: "五" },
  { value: 6, label: "六" },
  { value: 7, label: "日" },
];

const HISTORY_PAGE_SIZE = 10;

const localDate = () => {
  const date = new Date();
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
};

export const newSchedule = (config: AppConfig): ScheduleDefinition => ({
  id: `schedule_${Date.now()}`,
  name: "每日群聊导出",
  enabled: true,
  autoSyncSmartSheet: false,
  runAt: "18:30",
  weekdays: [1, 2, 3, 4, 5],
  dateMode: "today",
  fixedDate: localDate(),
  startTime: "00:00",
  endTime: "23:59",
  groups: [],
  ...defaultProcessingOptions(config),
});

const dateModeLabel: Record<ScheduleDateMode, string> = {
  today: "执行当天",
  yesterday: "执行前一天",
  fixed: "固定日期",
};

const scheduleRangeLabel = (schedule: Pick<ScheduleDefinition, "startTime" | "endTime">) =>
  schedule.endTime < schedule.startTime
    ? `${schedule.startTime}–次日 ${schedule.endTime}`
    : `${schedule.startTime}–${schedule.endTime}`;

type HistoryOutcomeSource = Pick<ScheduleExecutionHistoryItem, "success" | "result">
  & Partial<Pick<ScheduleExecutionHistoryItem, "status">>;

export function executionHistoryStatus(source: HistoryOutcomeSource): ScheduleExecutionStatus {
  if (["success", "partial", "empty", "failed"].includes(source.status ?? "")) {
    return source.status as ScheduleExecutionStatus;
  }
  if (["success", "partial", "empty", "failed"].includes(source.result?.status ?? "")) {
    return source.result?.status as ScheduleExecutionStatus;
  }
  return source.success ? "success" : "failed";
}

export function executionHistoryStatusLabel(source: HistoryOutcomeSource): string {
  const status = executionHistoryStatus(source);
  if (status === "partial") return "部分完成";
  if (status === "empty") return "无可分析记录";
  if (status === "failed") return "执行失败";
  if ((source.result?.emptyCount ?? 0) > 0) return "完成，部分群无记录";
  return "执行成功";
}

export function executionHistoryCounts(result?: TaskResult | null) {
  const runs = result?.runs ?? [];
  const count = (status: TaskRunResult["status"]) => runs.filter((run) => run.status === status).length;
  return {
    success: result?.successCount ?? count("success"),
    empty: result?.emptyCount ?? count("empty"),
    failed: result?.failedCount ?? count("failed"),
  };
}

export function automaticSyncWarningSummary(result?: TaskResult | null) {
  const warnedRuns = (result?.runs ?? []).filter((run) => (
    run.smartSheetSync?.status === "success"
    && Boolean(run.smartSheetSync.warning?.trim())
  ));
  return {
    groupCount: warnedRuns.length,
    missingImages: warnedRuns.reduce(
      (total, run) => total + (run.smartSheetSync?.missingImages ?? 0),
      0,
    ),
  };
}

export function isCurrentHistoryRequest(
  requestGeneration: number,
  currentGeneration: number,
): boolean {
  return requestGeneration === currentGeneration;
}

const historyDateLabel = (value: string) => {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleString("zh-CN", { hour12: false })
    : "时间未知";
};

const historyRunStatusLabel = (run: TaskRunResult) => {
  if (run.status === "empty") return "无聊天记录";
  if (run.status === "failed") return "处理失败";
  return "处理成功";
};

export function executionHistoryRunDetail(run: TaskRunResult): string {
  const runStatus = run.status ?? "success";
  const outputCount = Object.keys(run.outputs ?? {}).length;
  const runError = run.error
    ? toUserErrorMessage(run.error, "处理失败，请检查配置后重试。")
    : "";
  const automaticSyncError = run.smartSheetSync?.status === "failed" && run.smartSheetSync.error
    ? toUserErrorMessage(run.smartSheetSync.error, "腾讯文档自动同步失败。")
    : "";
  const error = runError || automaticSyncError;
  if (error) {
    return [error, outputCount > 0 ? `本地已生成 ${outputCount} 个文件` : ""]
      .filter(Boolean)
      .join(" · ");
  }
  if (runStatus !== "success") return historyRunStatusLabel(run);
  const automaticSyncLabel = run.smartSheetSync?.status === "success"
    ? run.smartSheetSync.synced !== undefined
      ? `已自动同步 ${run.smartSheetSync.synced} 条`
      : "已自动同步腾讯文档"
    : "";
  const automaticSyncWarning = run.smartSheetSync?.status === "success" && run.smartSheetSync.warning
    ? toUserErrorMessage(run.smartSheetSync.warning, "自动同步完成，但部分图片未就绪。")
    : "";
  return [
    historyRunStatusLabel(run),
    run.issueCount !== undefined ? `识别 ${run.issueCount} 个问题` : "",
    `${outputCount} 个文件`,
    automaticSyncLabel,
    automaticSyncWarning,
  ].filter(Boolean).join(" · ");
}

const processingFrom = (schedule: ScheduleDefinition): ProcessingOptions => ({
  promptId: schedule.promptId,
  smartSheetTemplateId: schedule.smartSheetTemplateId ?? "",
  runOcr: schedule.runOcr,
  runAnalysis: schedule.runAnalysis,
  exportXlsx: schedule.exportXlsx,
  exportMarkdown: schedule.exportMarkdown,
  prepareSmartSheet: schedule.prepareSmartSheet,
});

export function canAutoSyncSmartSheet(
  config: AppConfig,
  schedule: Pick<
    ScheduleDefinition,
    "runAnalysis" | "prepareSmartSheet" | "smartSheetTemplateId"
  >,
): boolean {
  const templateId = schedule.smartSheetTemplateId?.trim() ?? "";
  return schedule.runAnalysis
    && schedule.prepareSmartSheet
    && Boolean(templateId)
    && config.smart_sheet.templates.some((template) => template.id === templateId);
}

export function normalizeScheduleAutoSync(
  config: AppConfig,
  schedule: ScheduleDefinition,
): ScheduleDefinition {
  return {
    ...schedule,
    autoSyncSmartSheet: Boolean(
      schedule.autoSyncSmartSheet && canAutoSyncSmartSheet(config, schedule),
    ),
  };
}

export const scheduleForEditing = (config: AppConfig, schedule: ScheduleDefinition) => {
  const autoSyncSmartSheet = Boolean(
    schedule.autoSyncSmartSheet && canAutoSyncSmartSheet(config, schedule),
  );
  const smartSheetTemplateId = resolveSmartSheetTemplateId(
    config,
    schedule.promptId,
    schedule.smartSheetTemplateId,
  );
  return {
    ...schedule,
    autoSyncSmartSheet,
    smartSheetTemplateId,
    groups: [...schedule.groups],
    weekdays: [...schedule.weekdays],
  };
};

const pendingRunsFrom = (items: PendingScheduleSync[]): TaskRunResult[] => items
  .flatMap((item) => {
    if (item.result.runs?.length) return item.result.runs;
    const legacy = item.result as PendingScheduleSync["result"] & Partial<TaskRunResult>;
    if (!legacy.dayDir) return [];
    return [{
      groupId: legacy.groupId || item.scheduleId,
      groupName: legacy.groupName || item.scheduleName,
      dayDir: legacy.dayDir,
      outputs: legacy.outputs ?? {},
      startDate: legacy.startDate,
      endDate: legacy.endDate,
      startTime: legacy.startTime,
      endTime: legacy.endTime,
      smartSheetDate: legacy.smartSheetDate,
      smartSheetTemplateId: legacy.smartSheetTemplateId,
      smartSheetTemplateName: legacy.smartSheetTemplateName,
      smartSheetTemplateUrl: legacy.smartSheetTemplateUrl,
      definitionPath: legacy.definitionPath,
      smartSheetPreview: legacy.smartSheetPreview,
    }];
  })
  .filter((run) => (run.smartSheetPreview?.pending ?? 0) > 0);

const pendingCountFrom = (items: PendingScheduleSync[]) => pendingRunsFrom(items).reduce(
  (total, run) => total + (run.smartSheetPreview?.pending ?? 0),
  0,
);

const frozenTemplateId = (run: TaskRunResult) =>
  run.smartSheetTemplateId || run.smartSheetPreview?.template_id || "";

const frozenSyncDate = (run: TaskRunResult) =>
  run.smartSheetDate || run.endDate || run.startDate || "";

const frozenDefinitionPath = (run: TaskRunResult) => run.definitionPath?.trim() || "";

const hardBlockersFrom = (runs: TaskRunResult[]) => {
  const messages = new Set<string>();
  runs.forEach((run) => {
    if (!frozenTemplateId(run)) messages.add(`${run.groupName} 缺少冻结的腾讯文档模板 ID`);
    if (!frozenSyncDate(run)) messages.add(`${run.groupName} 缺少同步日期`);
    if (!frozenDefinitionPath(run)) messages.add(`${run.groupName} 缺少冻结的问题定义快照，旧结果只能放弃后重新执行`);
  });
  return [...messages];
};

const frozenTargetsFrom = (runs: TaskRunResult[]) => {
  const targets = new Map<string, {
    id: string;
    name: string;
    url: string;
    groups: Set<string>;
    pending: number;
  }>();
  runs.forEach((run) => {
    const id = frozenTemplateId(run);
    const name = run.smartSheetTemplateName || run.smartSheetPreview?.template_name || id || "未命名腾讯文档模板";
    const url = run.smartSheetTemplateUrl || run.smartSheetPreview?.template_url || "";
    const key = `${id}\u0000${name}\u0000${url}`;
    const target = targets.get(key) ?? { id, name, url, groups: new Set<string>(), pending: 0 };
    target.groups.add(run.groupName);
    target.pending += run.smartSheetPreview?.pending ?? 0;
    targets.set(key, target);
  });
  return [...targets.entries()].map(([key, target]) => ({
    key,
    id: target.id,
    name: target.name,
    url: target.url,
    groups: [...target.groups],
    pending: target.pending,
  }));
};

export interface OrphanPendingSyncGroup {
  scheduleId: string;
  scheduleName: string;
  latestCreatedAt: string;
  pendingCount: number;
  items: PendingScheduleSync[];
}

export interface PendingSyncBatch {
  scheduleId: string;
  items: PendingScheduleSync[];
  pendingIds: string[];
}

export function createPendingSyncBatch(
  scheduleId: string,
  pendingSyncs: PendingScheduleSync[],
): PendingSyncBatch {
  const items = pendingSyncs.filter((pending) => pending.scheduleId === scheduleId);
  return {
    scheduleId,
    items: [...items],
    pendingIds: items.map((pending) => pending.pendingId),
  };
}

export function withoutPendingSyncBatch(
  pendingSyncs: PendingScheduleSync[],
  batch: Pick<PendingSyncBatch, "pendingIds">,
): PendingScheduleSync[] {
  const cleared = new Set(batch.pendingIds);
  return pendingSyncs.filter((pending) => !cleared.has(pending.pendingId));
}

export function isCurrentConfirmationRequest(
  requestGeneration: number,
  currentGeneration: number,
): boolean {
  return requestGeneration === currentGeneration;
}

export function orphanPendingSyncGroupsFrom(
  schedules: Array<Pick<ScheduleDefinition, "id">>,
  pendingSyncs: PendingScheduleSync[],
): OrphanPendingSyncGroup[] {
  const currentIds = new Set(schedules.map((schedule) => schedule.id));
  const grouped = new Map<string, PendingScheduleSync[]>();
  pendingSyncs.forEach((pending) => {
    if (currentIds.has(pending.scheduleId)) return;
    grouped.set(pending.scheduleId, [...(grouped.get(pending.scheduleId) ?? []), pending]);
  });
  return [...grouped.entries()]
    .map(([scheduleId, items]) => ({
      scheduleId,
      scheduleName: items.at(-1)?.scheduleName || scheduleId,
      latestCreatedAt: items.at(-1)?.createdAt || "",
      pendingCount: pendingCountFrom(items),
      items,
    }))
    .sort((left, right) => right.latestCreatedAt.localeCompare(left.latestCreatedAt));
}

export function SchedulesPage({ config }: { config: AppConfig }) {
  const [schedules, setSchedules] = useState<ScheduleDefinition[]>(config.schedules ?? []);
  const [groups, setGroups] = useState<GroupInfo[]>([]);
  const [loadingGroups, setLoadingGroups] = useState(false);
  const [saving, setSaving] = useState(false);
  const [runningId, setRunningId] = useState("");
  const [editor, setEditor] = useState<ScheduleDefinition | null>(null);
  const [activity, setActivity] = useState<Record<string, string>>({});
  const [pendingSyncs, setPendingSyncs] = useState<PendingScheduleSync[]>([]);
  const [confirmingBatch, setConfirmingBatch] = useState<PendingSyncBatch | null>(null);
  const [refreshedConfirmingRuns, setRefreshedConfirmingRuns] = useState<TaskRunResult[] | null>(null);
  const [refreshingConfirmation, setRefreshingConfirmation] = useState(false);
  const [confirmationRefreshError, setConfirmationRefreshError] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyScheduleId, setHistoryScheduleId] = useState("");
  const [historyData, setHistoryData] = useState<ScheduleExecutionHistoryPage | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [historyRefreshRevision, setHistoryRefreshRevision] = useState(0);
  const confirmationRequestGeneration = useRef(0);
  const historyRequestGeneration = useRef(0);

  const refreshPendingSyncs = (showError = true) => bridge.listPendingSmartSheetSyncs()
    .then(setPendingSyncs)
    .catch((error) => {
      if (!showError) return;
      toast.error("无法读取腾讯文档待确认结果", {
        description: toUserErrorMessage(error, "请重新打开应用后重试。"),
      });
    });

  useEffect(() => {
    void bridge.listSchedules().then(setSchedules).catch((error) => {
      toast.error("无法读取定时任务", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
    });
    void refreshPendingSyncs();
    let disposed = false;
    const cleanups: Array<() => void> = [];
    void bridge.onScheduleProgress((event) => {
      if (!disposed) {
        const message = toUserErrorMessage(event.message, "任务正在处理中…");
        setActivity((current) => ({ ...current, [event.scheduleId]: message }));
      }
    }).then((cleanup) => cleanups.push(cleanup)).catch((error) => {
      toast.error("定时任务状态监听异常", {
        description: toUserErrorMessage(error, "请重新打开应用后重试。"),
      });
    });
    void bridge.onScheduleCompleted((event) => {
      if (disposed) return;
      const message = event.success === false
        ? toUserErrorMessage(event.message, "任务未完成，请检查配置后重试。")
        : toUserErrorMessage(event.message, "任务已完成");
      const syncWarnings = automaticSyncWarningSummary(event.result);
      setActivity((current) => ({ ...current, [event.scheduleId]: message }));
      setRunningId("");
      setHistoryPage(1);
      setHistoryRefreshRevision((current) => current + 1);
      if (event.success !== false) void refreshPendingSyncs(false);
      if (event.success === false) {
        toast.error(`${event.scheduleName}执行失败`, {
          description: message,
        });
      }
      else if (event.historyPersisted === false) {
        toast.warning(`${event.scheduleName} 已完成，但执行记录保存失败`, {
          description: message,
        });
      }
      else if (event.result?.status === "partial") {
        toast.warning(`${event.scheduleName} 部分完成`, { description: message });
      }
      else if (event.result?.status === "empty") {
        toast.warning(`${event.scheduleName} 没有可分析记录`, { description: message });
      }
      else if (syncWarnings.groupCount > 0) {
        toast.warning(`${event.scheduleName} 已同步，部分图片未就绪`, {
          description: message,
        });
      }
      else if ((event.result?.emptyCount ?? 0) > 0) {
        toast.warning(`${event.scheduleName} 执行完成，部分群无记录`, {
          description: `${event.result?.emptyCount ?? 0} 个群聊已跳过，其余群聊已正常处理。`,
        });
      }
      else toast.success(`${event.scheduleName} 执行完成`);
    }).then((cleanup) => cleanups.push(cleanup)).catch((error) => {
      toast.error("定时任务结果监听异常", {
        description: toUserErrorMessage(error, "请重新打开应用后重试。"),
      });
    });
    return () => {
      disposed = true;
      confirmationRequestGeneration.current += 1;
      historyRequestGeneration.current += 1;
      cleanups.forEach((cleanup) => cleanup());
    };
  }, []);

  useEffect(() => {
    if (!historyOpen) return;
    const requestGeneration = historyRequestGeneration.current + 1;
    historyRequestGeneration.current = requestGeneration;
    setHistoryLoading(true);
    setHistoryError("");
    void bridge.listScheduleExecutionHistory(
      historyPage,
      HISTORY_PAGE_SIZE,
      historyScheduleId || undefined,
    ).then((response) => {
      if (!isCurrentHistoryRequest(requestGeneration, historyRequestGeneration.current)) return;
      const normalizedPage = response.totalPages > 0
        ? Math.min(response.page, response.totalPages)
        : 1;
      if (normalizedPage !== response.page) {
        setHistoryData(null);
        setHistoryPage(normalizedPage);
        return;
      }
      setHistoryData(response);
      if (response.page !== historyPage) setHistoryPage(response.page);
    }).catch((error) => {
      if (!isCurrentHistoryRequest(requestGeneration, historyRequestGeneration.current)) return;
      setHistoryError(toUserErrorMessage(error, "请稍后重试。"));
    }).finally(() => {
      if (isCurrentHistoryRequest(requestGeneration, historyRequestGeneration.current)) {
        setHistoryLoading(false);
      }
    });
  }, [historyOpen, historyPage, historyRefreshRevision, historyScheduleId]);

  const enabledCount = schedules.filter((schedule) => schedule.enabled).length;
  const sortedSchedules = useMemo(
    () => [...schedules].sort((left, right) => left.runAt.localeCompare(right.runAt)),
    [schedules],
  );
  const pendingBySchedule = useMemo(() => {
    const grouped = new Map<string, PendingScheduleSync[]>();
    pendingSyncs.forEach((pending) => {
      grouped.set(pending.scheduleId, [...(grouped.get(pending.scheduleId) ?? []), pending]);
    });
    return grouped;
  }, [pendingSyncs]);
  const orphanPendingGroups = useMemo(
    () => orphanPendingSyncGroupsFrom(schedules, pendingSyncs),
    [pendingSyncs, schedules],
  );
  const confirmingScheduleId = confirmingBatch?.scheduleId ?? "";
  const confirmingSyncs = confirmingBatch?.items ?? [];
  const confirmingRuns = refreshedConfirmingRuns ?? [];
  const confirmingPendingCount = confirmingRuns.reduce(
    (total, run) => total + (run.smartSheetPreview?.pending ?? 0),
    0,
  );
  const confirmingTargets = frozenTargetsFrom(confirmingRuns);
  const confirmingWarnings = smartSheetConfigurationBlockers(confirmingRuns);
  const confirmingBlockers = [...new Set([
    ...hardBlockersFrom(confirmingRuns),
    ...(refreshedConfirmingRuns
      ? confirmingRuns
        .flatMap((run) => [
          !run.smartSheetPreview?.template_revision
            ? `${run.groupName} 的当前模板缺少 revision，无法安全确认写入`
            : "",
          !run.smartSheetPreview?.document_revision
            ? `${run.groupName} 的问题定义快照缺少 document revision，无法安全确认写入`
            : "",
        ])
        .filter(Boolean)
      : []),
  ])];
  const confirmingScheduleName = confirmingSyncs[0]?.scheduleName
    || schedules.find((schedule) => schedule.id === confirmingScheduleId)?.name
    || "定时任务";
  const autoSyncAvailable = editor ? canAutoSyncSmartSheet(config, editor) : false;

  const loadGroups = async () => {
    setLoadingGroups(true);
    try {
      const response = await bridge.listGroups();
      setGroups(response.groups);
      toast.success(`已读取 ${response.groups.length} 个群聊`);
    } catch (error) {
      toast.error("无法读取群聊", {
        description: toUserErrorMessage(error, "请确认企业微信数据目录和密钥配置正确。"),
      });
    } finally {
      setLoadingGroups(false);
    }
  };

  const persist = async (next: ScheduleDefinition[], message?: string) => {
    setSaving(true);
    try {
      const saved = await bridge.saveSchedules(next);
      setSchedules(saved);
      if (message) toast.success(message);
    } catch (error) {
      toast.error("定时任务保存失败", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
      throw error;
    } finally {
      setSaving(false);
    }
  };

  const saveEditor = async () => {
    if (!editor) return;
    if (!editor.name.trim()) return toast.warning("请输入任务名称");
    if (!editor.groups.length) return toast.warning("请至少选择一个群聊");
    if (!editor.weekdays.length) return toast.warning("请至少选择一个执行日");
    if (editor.endTime < editor.startTime && editor.dateMode === "today") {
      return toast.warning("跨夜任务请选择“执行前一天”，确保结束时间在任务执行当天");
    }
    if (editor.prepareSmartSheet && !editor.runAnalysis) return toast.warning("准备 Smart Sheet 前需要启用大模型分析");
    const smartSheetTemplateId = resolveSmartSheetTemplateId(config, editor.promptId, editor.smartSheetTemplateId);
    if (editor.prepareSmartSheet && !smartSheetTemplateId) return toast.warning("请选择腾讯文档模板");
    const normalizedEditor = normalizeScheduleAutoSync(config, {
      ...editor,
      smartSheetTemplateId,
    });
    const exists = schedules.some((schedule) => schedule.id === editor.id);
    const next = exists
      ? schedules.map((schedule) => schedule.id === editor.id ? normalizedEditor : schedule)
      : [...schedules, normalizedEditor];
    try {
      await persist(next, exists ? "定时任务已更新" : "定时任务已创建");
      setEditor(null);
    } catch {
      // Error toast is handled by persist.
    }
  };

  const toggleEnabled = async (schedule: ScheduleDefinition) => {
    const next = schedules.map((item) => item.id === schedule.id ? { ...item, enabled: !item.enabled } : item);
    try {
      await persist(next);
    } catch {
      // Error toast is handled by persist.
    }
  };

  const remove = async (schedule: ScheduleDefinition) => {
    if (runningId === schedule.id) {
      toast.warning("任务正在运行，请等待完成后再删除");
      return;
    }
    if ((pendingBySchedule.get(schedule.id)?.length ?? 0) > 0) {
      toast.warning("请先同步或放弃该任务的腾讯文档待确认结果，再删除任务");
      return;
    }
    if (!window.confirm(`确定删除“${schedule.name}”吗？`)) return;
    try {
      await persist(schedules.filter((item) => item.id !== schedule.id), "定时任务已删除");
    } catch {
      // Error toast is handled by persist.
    }
  };

  const runNow = async (schedule: ScheduleDefinition) => {
    setRunningId(schedule.id);
    setActivity((current) => ({ ...current, [schedule.id]: "正在准备立即执行…" }));
    try {
      await bridge.runScheduleNow(schedule.id);
    } catch (error) {
      setRunningId("");
      toast.error("无法启动任务", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
    }
  };

  const openHistory = () => {
    setHistoryData(null);
    setHistoryError("");
    setHistoryPage(1);
    setHistoryOpen(true);
  };

  const closeHistory = () => {
    historyRequestGeneration.current += 1;
    setHistoryOpen(false);
    setHistoryLoading(false);
    setHistoryError("");
  };

  const filterHistory = (scheduleId: string) => {
    historyRequestGeneration.current += 1;
    setHistoryData(null);
    setHistoryError("");
    setHistoryPage(1);
    setHistoryScheduleId(scheduleId);
  };

  const changeHistoryPage = (page: number) => {
    if (historyLoading || page < 1 || page === historyPage) return;
    setHistoryError("");
    setHistoryPage(page);
  };

  const refreshHistory = () => {
    historyRequestGeneration.current += 1;
    setHistoryError("");
    setHistoryRefreshRevision((current) => current + 1);
  };

  const openHistoryResult = async (path: string) => {
    if (!path) return;
    try {
      await bridge.openPath(path);
    } catch (error) {
      toast.error("无法打开执行结果", {
        description: toUserErrorMessage(error, "本地结果可能已被移动或删除。"),
      });
    }
  };

  const closePendingConfirmation = () => {
    confirmationRequestGeneration.current += 1;
    setConfirmingBatch(null);
    setRefreshedConfirmingRuns(null);
    setConfirmationRefreshError("");
    setRefreshingConfirmation(false);
  };

  const openPendingConfirmation = async (scheduleId: string) => {
    const batch = createPendingSyncBatch(scheduleId, pendingSyncs);
    if (!batch.items.length) return;
    const requestGeneration = confirmationRequestGeneration.current + 1;
    confirmationRequestGeneration.current = requestGeneration;
    const frozenRuns = pendingRunsFrom(batch.items);
    setConfirmingBatch(batch);
    setRefreshedConfirmingRuns(null);
    setConfirmationRefreshError("");
    setRefreshingConfirmation(true);
    try {
      const frozenBlockers = hardBlockersFrom(frozenRuns);
      if (frozenBlockers.length) throw new Error(frozenBlockers.join("；"));
      const refreshedRuns = await Promise.all(frozenRuns.map(async (run) => {
        const preview = await bridge.previewSmartSheet(
          run.dayDir,
          frozenSyncDate(run),
          frozenTemplateId(run),
          frozenDefinitionPath(run),
        );
        return {
          ...run,
          smartSheetTemplateId: preview.template_id || frozenTemplateId(run),
          smartSheetTemplateName: preview.template_name ?? run.smartSheetTemplateName,
          smartSheetTemplateUrl: preview.template_url ?? run.smartSheetTemplateUrl,
          smartSheetPreview: preview,
        };
      }));
      if (!isCurrentConfirmationRequest(
        requestGeneration,
        confirmationRequestGeneration.current,
      )) return;
      setRefreshedConfirmingRuns(refreshedRuns);
    } catch (error) {
      if (!isCurrentConfirmationRequest(
        requestGeneration,
        confirmationRequestGeneration.current,
      )) return;
      const message = toUserErrorMessage(error, "无法按当前配置刷新腾讯文档预览，请修复后重试。");
      setConfirmationRefreshError(message);
      toast.error("待确认预览刷新失败", { description: message });
    } finally {
      if (isCurrentConfirmationRequest(
        requestGeneration,
        confirmationRequestGeneration.current,
      )) {
        setRefreshingConfirmation(false);
      }
    }
  };

  const confirmPendingSync = async () => {
    if (
      !confirmingSyncs.length
      || refreshedConfirmingRuns === null
      || refreshingConfirmation
      || confirmationRefreshError
      || confirmingWarnings.length
      || confirmingBlockers.length
    ) return;
    const batch = confirmingBatch;
    if (!batch) return;
    const pendingIds = batch.pendingIds;
    const runsToSync = confirmingRuns.filter((run) => (run.smartSheetPreview?.pending ?? 0) > 0);
    setSyncing(true);
    try {
      let synced = 0;
      for (const run of runsToSync) {
        const response = await bridge.syncSmartSheet(
          run.dayDir,
          frozenSyncDate(run),
          frozenTemplateId(run),
          true,
          run.smartSheetPreview?.template_revision || "",
          frozenDefinitionPath(run),
          run.smartSheetPreview?.document_revision || "",
        );
        synced += response.synced ?? run.smartSheetPreview?.pending ?? 0;
      }
      await bridge.clearPendingSmartSheetSyncs(pendingIds);
      setPendingSyncs((current) => withoutPendingSyncBatch(current, batch));
      closePendingConfirmation();
      toast.success(runsToSync.length
        ? `腾讯文档同步完成，共写入 ${synced} 条问题`
        : "当前已无待写入问题，待确认结果已清除");
    } catch (error) {
      toast.error("腾讯文档同步未全部完成", {
        description: toUserErrorMessage(error, "待确认结果已保留。中断后请先核对目标文档；仅已取得远端记录 ID 且成功落入本地台账的记录会在重试时跳过。"),
      });
    } finally {
      setSyncing(false);
    }
  };

  const discardPendingItems = async (items: PendingScheduleSync[], scheduleName: string) => {
    if (!items.length) return;
    if (!window.confirm(`确定放弃“${scheduleName}”的待确认同步结果吗？本地导出文件不会被删除。`)) return;
    const batch: PendingSyncBatch = {
      scheduleId: items[0].scheduleId,
      items: [...items],
      pendingIds: items.map((pending) => pending.pendingId),
    };
    setSyncing(true);
    try {
      await bridge.clearPendingSmartSheetSyncs(batch.pendingIds);
      setPendingSyncs((current) => withoutPendingSyncBatch(current, batch));
      closePendingConfirmation();
      toast.success("已放弃该任务的腾讯文档待确认结果");
    } catch (error) {
      toast.error("无法清除待确认结果", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
    } finally {
      setSyncing(false);
    }
  };

  const discardPendingSync = () => discardPendingItems(confirmingSyncs, confirmingScheduleName);

  const updateEditor = (patch: Partial<ScheduleDefinition>) => {
    setEditor((current) => current ? { ...current, ...patch } : current);
  };

  const updateProcessing = (processing: ProcessingOptions) => {
    setEditor((current) => current
      ? normalizeScheduleAutoSync(config, { ...current, ...processing })
      : current);
  };
  const updateWeekday = (day: number) => {
    if (!editor) return;
    updateEditor({
      weekdays: editor.weekdays.includes(day)
        ? editor.weekdays.filter((value) => value !== day)
        : [...editor.weekdays, day].sort(),
    });
  };

  return (
    <div className="page-content schedules-page">
      <div className="page-title-row">
        <div>
          <div className="eyebrow"><CalendarClock size={13} />Automation</div>
          <h1>定时导出</h1>
          <p>按设定时间自动处理多个群聊，动态选择当天或前一天记录。</p>
        </div>
        <div className="schedule-title-actions">
          <Button variant="secondary" onClick={openHistory}><History size={16} />执行记录</Button>
          <Button onClick={() => setEditor(newSchedule(config))}><Plus size={16} />新建定时任务</Button>
        </div>
      </div>

      <div className="schedule-overview">
        <div className="glass-card schedule-stat"><span><Power size={17} /></span><div><strong>{enabledCount}</strong><small>正在运行的任务</small></div></div>
        <div className="glass-card schedule-stat"><span><CalendarClock size={17} /></span><div><strong>{schedules.length}</strong><small>全部定时任务</small></div></div>
        <div className="scheduler-note"><Info size={16} /><span>当前版本需保持应用运行；到点后任务在后台自动执行，每个群结果独立保存。</span></div>
      </div>

      {orphanPendingGroups.length > 0 && (
        <section className="glass-card schedule-list-card">
          <SectionHeader
            title="已删除任务的待确认结果"
            description="任务可能在运行期间被删除；本地结果仍完整保留，需要你确认写入或主动放弃。"
          />
          <div className="schedule-list">
            {orphanPendingGroups.map((group) => {
              const createdAt = group.latestCreatedAt ? new Date(group.latestCreatedAt) : null;
              const createdAtLabel = createdAt && !Number.isNaN(createdAt.getTime())
                ? createdAt.toLocaleString("zh-CN", { hour12: false })
                : "完成时间未知";
              return (
                <article className="schedule-row" key={group.scheduleId}>
                  <div className="schedule-power"><CloudUpload size={15} /></div>
                  <div className="schedule-time"><strong>{group.pendingCount}</strong><small>条待写入</small></div>
                  <div className="schedule-main">
                    <strong>{group.scheduleName}</strong>
                    <div className="schedule-meta">
                      <span>{group.items.length} 次待确认运行</span>
                      <span><Clock3 size={12} />{createdAtLabel}</span>
                    </div>
                  </div>
                  <div className="schedule-actions">
                    <Button variant="secondary" disabled={syncing || refreshingConfirmation} onClick={() => void openPendingConfirmation(group.scheduleId)}>
                      <CloudUpload size={14} />确认同步
                    </Button>
                    <Button variant="danger" disabled={syncing} onClick={() => void discardPendingItems(group.items, group.scheduleName)}>
                      放弃
                    </Button>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}

      <section className="glass-card schedule-list-card">
        <SectionHeader title="任务列表" description="开关关闭后会保留配置，但不会自动执行。" />
        {sortedSchedules.length ? (
          <div className="schedule-list">
            {sortedSchedules.map((schedule) => {
              const schedulePendingSyncs = pendingBySchedule.get(schedule.id) ?? [];
              const schedulePendingCount = pendingCountFrom(schedulePendingSyncs);
              return (
                <article className={schedule.enabled ? "schedule-row" : "schedule-row disabled"} key={schedule.id}>
                <button
                  type="button"
                  className={schedule.enabled ? "schedule-power enabled" : "schedule-power"}
                  title={schedule.enabled ? "暂停任务" : "启用任务"}
                  disabled={saving}
                  onClick={() => void toggleEnabled(schedule)}
                >
                  <Power size={15} />
                </button>
                <div className="schedule-time"><strong>{schedule.runAt}</strong><small>{schedule.enabled ? "已启用" : "已暂停"}</small></div>
                <div className="schedule-main">
                  <strong>{schedule.name}</strong>
                  <div className="schedule-meta">
                    <span><UsersRound size={12} />{schedule.groups.length} 个群</span>
                    <span><Clock3 size={12} />{scheduleRangeLabel(schedule)}</span>
                    <span>{dateModeLabel[schedule.dateMode]}</span>
                    {schedule.prepareSmartSheet && <span>{config.smart_sheet.templates.find((template) => template.id === schedule.smartSheetTemplateId)?.name ?? "腾讯模板待确认"}</span>}
                    <span>周{schedule.weekdays.map((day) => weekdays.find((item) => item.value === day)?.label).join("、")}</span>
                  </div>
                  {activity[schedule.id] && <p className="schedule-activity">{activity[schedule.id]}</p>}
                </div>
                <div className="schedule-actions">
                  {schedulePendingCount > 0 && (
                    <Button
                      className="schedule-confirm-sync"
                      variant="secondary"
                      disabled={syncing || refreshingConfirmation}
                      onClick={() => void openPendingConfirmation(schedule.id)}
                    >
                      <CloudUpload size={14} />确认同步 {schedulePendingCount}
                    </Button>
                  )}
                  <Button variant="secondary" disabled={runningId === schedule.id} onClick={() => void runNow(schedule)}>
                    {runningId === schedule.id ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}立即执行
                  </Button>
                  <Button variant="ghost" title="编辑" onClick={() => setEditor(scheduleForEditing(config, schedule))}><Edit3 size={15} /></Button>
                  <Button variant="ghost" title="删除" disabled={runningId === schedule.id} onClick={() => void remove(schedule)}><Trash2 size={15} /></Button>
                </div>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="schedule-empty">
            <div><CalendarClock size={27} /></div>
            <strong>还没有定时任务</strong>
            <p>创建后，应用会在指定日期和时间自动导出群聊。</p>
            <Button variant="secondary" onClick={() => setEditor(newSchedule(config))}><Plus size={15} />创建第一个任务</Button>
          </div>
        )}
      </section>

      {historyOpen && (
        <div className="modal-backdrop schedule-modal-backdrop">
          <div className="schedule-modal schedule-history-modal" role="dialog" aria-modal="true" aria-labelledby="schedule-history-title">
            <div className="schedule-modal-header">
              <div>
                <span className="eyebrow">Execution history</span>
                <h2 id="schedule-history-title">定时任务执行记录</h2>
              </div>
              <button type="button" aria-label="关闭执行记录" onClick={closeHistory}>×</button>
            </div>
            <div className="schedule-history-toolbar">
              <label>
                <span>筛选任务</span>
                <select
                  className="input"
                  value={historyScheduleId}
                  onChange={(event) => filterHistory(event.target.value)}
                >
                  <option value="">全部定时任务</option>
                  {sortedSchedules.map((schedule) => (
                    <option key={schedule.id} value={schedule.id}>{schedule.name}</option>
                  ))}
                </select>
              </label>
              <Button variant="ghost" disabled={historyLoading} onClick={refreshHistory}>
                <RefreshCw size={14} className={historyLoading ? "spin" : undefined} />刷新
              </Button>
            </div>
            <div className="schedule-modal-body schedule-history-body">
              {historyLoading && !historyData ? (
                <div className="schedule-history-state">
                  <LoaderCircle size={25} className="spin" />
                  <strong>正在读取执行记录…</strong>
                  <span>记录保存在本机，不会触发新的任务。</span>
                </div>
              ) : historyError ? (
                <div className="schedule-history-state schedule-history-error">
                  <AlertCircle size={25} />
                  <strong>执行记录读取失败</strong>
                  <span>{historyError}</span>
                  <Button variant="secondary" onClick={refreshHistory}>重新加载</Button>
                </div>
              ) : historyData && historyData.items.length === 0 ? (
                <div className="schedule-history-state">
                  <History size={27} />
                  <strong>{historyScheduleId ? "该任务还没有执行记录" : "还没有执行记录"}</strong>
                  <span>任务完成后，结果会自动保存在这里。</span>
                </div>
              ) : (
                <div className={historyLoading ? "schedule-history-list is-loading" : "schedule-history-list"}>
                  {historyData?.items.map((item) => {
                    const status = executionHistoryStatus(item);
                    const statusLabel = executionHistoryStatusLabel(item);
                    const counts = executionHistoryCounts(item.result);
                    const runs = item.result?.runs ?? [];
                    const itemMessage = toUserErrorMessage(
                      item.message,
                      item.success ? "任务执行完成" : "任务执行失败，请检查配置后重试。",
                    );
                    const hasDetailedCounts = runs.some((run) => Boolean(run.status))
                      || item.result?.successCount !== undefined
                      || item.result?.emptyCount !== undefined
                      || item.result?.failedCount !== undefined;
                    return (
                      <article className="schedule-history-item" key={item.executionId}>
                        <div className={`schedule-history-status history-status-${status}`}>
                          {status === "success" && <CheckCircle2 size={18} />}
                          {status === "partial" && <AlertCircle size={18} />}
                          {status === "empty" && <CircleMinus size={18} />}
                          {status === "failed" && <XCircle size={18} />}
                        </div>
                        <div className="schedule-history-main">
                          <div className="schedule-history-heading">
                            <div>
                              <strong>{item.scheduleName || item.scheduleId}</strong>
                              <span className={`history-status-badge history-status-${status}`}>{statusLabel}</span>
                            </div>
                            <time dateTime={item.finishedAt}>{historyDateLabel(item.finishedAt)}</time>
                          </div>
                          <div className="schedule-history-meta">
                            <span>{item.trigger === "manual" ? "手动执行" : "自动执行"}</span>
                            {item.startedAt && <span>开始于 {historyDateLabel(item.startedAt)}</span>}
                            {hasDetailedCounts && <span>成功 {counts.success} · 无记录 {counts.empty} · 失败 {counts.failed}</span>}
                          </div>
                          <p>{itemMessage}</p>
                          {runs.length > 0 && (
                            <details className="schedule-history-details">
                              <summary><UsersRound size={13} />查看 {runs.length} 个群的处理结果</summary>
                              <div className="schedule-history-groups">
                                {runs.map((run, index) => {
                                  const runStatus = run.status ?? "success";
                                  const outputCount = Object.keys(run.outputs ?? {}).length;
                                  const runDetail = executionHistoryRunDetail(run);
                                  return (
                                    <div className={`schedule-history-group history-group-${runStatus}`} key={`${run.groupId}-${index}`}>
                                      <span className="schedule-history-group-icon">
                                        {runStatus === "success" && <CheckCircle2 size={14} />}
                                        {runStatus === "empty" && <CircleMinus size={14} />}
                                        {runStatus === "failed" && <XCircle size={14} />}
                                      </span>
                                      <span className="schedule-history-group-main">
                                        <strong>{run.groupName || run.groupId}</strong>
                                        <small>{runDetail}</small>
                                      </span>
                                      {run.dayDir && (runStatus === "success" || outputCount > 0) && (
                                        <button type="button" title="打开本地结果目录" onClick={() => void openHistoryResult(run.dayDir)}>
                                          <FolderOpen size={14} />
                                        </button>
                                      )}
                                    </div>
                                  );
                                })}
                              </div>
                            </details>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}
            </div>
            <div className="schedule-modal-footer schedule-history-footer">
              <span>
                共 {historyData?.total ?? 0} 次
                {historyData && historyData.totalPages > 0
                  ? ` · 第 ${historyData.page} / ${historyData.totalPages} 页`
                  : ""}
              </span>
              <Button
                variant="secondary"
                disabled={historyLoading || !historyData || historyData.page <= 1}
                onClick={() => changeHistoryPage((historyData?.page ?? historyPage) - 1)}
              >
                <ChevronLeft size={14} />上一页
              </Button>
              <Button
                variant="secondary"
                disabled={historyLoading || !historyData || historyData.page >= historyData.totalPages}
                onClick={() => changeHistoryPage((historyData?.page ?? historyPage) + 1)}
              >
                下一页<ChevronRight size={14} />
              </Button>
            </div>
          </div>
        </div>
      )}

      {editor && (
        <div className="modal-backdrop schedule-modal-backdrop">
          <div className="schedule-modal">
            <div className="schedule-modal-header">
              <div><span className="eyebrow">Schedule editor</span><h2>{schedules.some((item) => item.id === editor.id) ? "编辑定时任务" : "新建定时任务"}</h2></div>
              <button type="button" onClick={() => setEditor(null)}>×</button>
            </div>
            <div className="schedule-modal-body">
              <section className="schedule-editor-section">
                <h3>基本信息</h3>
                <div className="form-grid two-columns">
                  <Field label="任务名称"><Input value={editor.name} onChange={(event) => updateEditor({ name: event.target.value })} /></Field>
                  <Field label="每天执行时间"><Input type="time" value={editor.runAt} onChange={(event) => updateEditor({ runAt: event.target.value })} /></Field>
                </div>
                <Field label="每周执行日">
                  <div className="weekday-picker">
                    {weekdays.map((day) => <button type="button" key={day.value} className={editor.weekdays.includes(day.value) ? "selected" : ""} onClick={() => updateWeekday(day.value)}>周{day.label}</button>)}
                  </div>
                </Field>
              </section>

              <section className="schedule-editor-section">
                <h3>导出时间范围</h3>
                <div className="date-mode-picker">
                  {(["today", "yesterday", "fixed"] as ScheduleDateMode[]).map((mode) => (
                    <button type="button" key={mode} className={editor.dateMode === mode ? "selected" : ""} onClick={() => updateEditor({ dateMode: mode })}>
                      <strong>{dateModeLabel[mode]}</strong>
                      <small>{mode === "today" ? "每天动态取当天" : mode === "yesterday" ? "跨夜任务请选此项" : "始终导出指定日期"}</small>
                    </button>
                  ))}
                </div>
                <div className="form-grid schedule-range-grid">
                  {editor.dateMode === "fixed" && <Field label="固定日期"><Input type="date" value={editor.fixedDate} onChange={(event) => updateEditor({ fixedDate: event.target.value })} /></Field>}
                  <Field label="开始时间"><Input type="time" value={editor.startTime} onChange={(event) => updateEditor({ startTime: event.target.value })} /></Field>
                  <Field label="结束时间" hint="早于开始时间时按次日处理"><Input type="time" value={editor.endTime} onChange={(event) => updateEditor({ endTime: event.target.value })} /></Field>
                </div>
              </section>

              <section className="schedule-editor-section">
                <div className="schedule-section-heading"><h3>执行群聊</h3><span>已选 {editor.groups.length} 个</span></div>
                <GroupMultiSelect groups={groups} selected={editor.groups} loading={loadingGroups} onReload={() => void loadGroups()} onChange={(next: TaskGroup[]) => updateEditor({ groups: next })} />
              </section>

              <section className="schedule-editor-section">
                <h3>处理方式</h3>
                <ProcessingOptionsEditor
                  config={config}
                  value={processingFrom(editor)}
                  onChange={updateProcessing}
                  smartSheetHint={editor.autoSyncSmartSheet
                    ? "任务完成后自动写入腾讯文档"
                    : "默认仅生成待同步预览，由你确认后写入"}
                />
                <div className="schedule-auto-sync">
                  <Switch
                    checked={Boolean(editor.autoSyncSmartSheet)}
                    disabled={!autoSyncAvailable}
                    onChange={(autoSyncSmartSheet) => updateEditor({
                      autoSyncSmartSheet: autoSyncSmartSheet && autoSyncAvailable,
                    })}
                    label="自动同步腾讯文档"
                    description={autoSyncAvailable
                      ? "无需手动确认；任务完成后直接写入已选模板"
                      : "需先开启大模型分析和 Smart Sheet，并选择有效模板"}
                  />
                  <p className="schedule-auto-sync-risk">
                    <AlertCircle size={14} />
                    <span>这是外部写入操作。启用后定时任务到点会直接写入腾讯文档，不再等待人工确认；建议先手动执行一次并核对字段映射。</span>
                  </p>
                </div>
              </section>

              <Switch checked={editor.enabled} onChange={(enabled) => updateEditor({ enabled })} label="保存后立即启用" description="关闭时仅保存任务配置" />
            </div>
            <div className="schedule-modal-footer">
              <Button variant="secondary" onClick={() => setEditor(null)}>取消</Button>
              <Button disabled={saving} onClick={() => void saveEditor()}>{saving && <LoaderCircle size={15} className="spin" />}保存任务</Button>
            </div>
          </div>
        </div>
      )}

      {confirmingScheduleId && confirmingSyncs.length > 0 && (
        <div className="modal-backdrop">
          <div className="modal-card schedule-sync-modal smart-sheet-sync-modal">
            <div className="modal-icon"><CloudUpload size={24} /></div>
            <h2>确认写入腾讯文档？</h2>
            {refreshingConfirmation ? (
              <p><LoaderCircle className="spin" size={15} /> 正在按当前配置刷新每个群的腾讯文档预览…</p>
            ) : confirmationRefreshError ? (
              <p>定时任务“{confirmingScheduleName}”的当前预览刷新失败，本次确认已被阻止。</p>
            ) : (
              <p>
                定时任务“{confirmingScheduleName}”的 <strong>{confirmingRuns.length}</strong> 个群共有{" "}
                <strong>{confirmingPendingCount}</strong> 条新问题待写入。所有群同步成功后，待确认结果才会清除。
              </p>
            )}
            {!refreshingConfirmation && !confirmationRefreshError && (
              <div className="sync-template-list">
                {confirmingTargets.map((target) => (
                  <div key={target.key}>
                    <span><Table2 size={14} /></span>
                    <span>
                      <strong>{target.name} · {target.pending} 条</strong>
                      <small>{target.groups.join("、")}</small>
                      <small>{target.url || "未提供腾讯文档模板 URL"}</small>
                    </span>
                  </div>
                ))}
              </div>
            )}
            {confirmationRefreshError && (
              <div className="sync-blockers"><span>{confirmationRefreshError}</span></div>
            )}
            {confirmingWarnings.length > 0 && (
              <div className="sync-blockers schedule-sync-warnings">
                {confirmingWarnings.map((message) => <span key={message}>{message}</span>)}
                <small>以上是确认前刷新得到的当前状态；尝试写入时 Worker 仍会再次校验。</small>
              </div>
            )}
            {confirmingBlockers.length > 0 && (
              <div className="sync-blockers">
                {confirmingBlockers.map((message) => <span key={message}>{message}</span>)}
              </div>
            )}
            <div className="modal-note">
              {refreshingConfirmation
                ? "刷新预览只读取当前本地结果和模板配置，不会向腾讯文档写入数据。"
                : confirmationRefreshError
                  ? "待确认结果已保留。修复模板配置后关闭弹窗并重新打开，即可再次刷新。"
                  : confirmingWarnings.length
                    ? "当前字段映射或 Webhook 配置无效，请先到设置中修复并保存，再重新打开确认窗口。"
                  : confirmingBlockers.length
                ? "冻结结果缺少同步所需信息，无法自动写入；可放弃此待确认结果后重新执行任务。"
                : "这是外部写入操作。中断后重试前请先核对目标文档；仅已取得远端记录 ID 且成功落入本地台账的记录会跳过。"}
            </div>
            <div className="modal-actions schedule-sync-modal-actions">
              <Button variant="danger" disabled={syncing || refreshingConfirmation} onClick={() => void discardPendingSync()}>
                放弃待确认
              </Button>
              <span />
              <Button variant="secondary" disabled={syncing} onClick={closePendingConfirmation}>取消</Button>
              <Button
                disabled={
                  syncing
                  || refreshingConfirmation
                  || refreshedConfirmingRuns === null
                  || Boolean(confirmationRefreshError)
                  || confirmingWarnings.length > 0
                  || confirmingBlockers.length > 0
                }
                onClick={() => void confirmPendingSync()}
              >
                {syncing && <LoaderCircle className="spin" size={16} />}
                {confirmingPendingCount > 0 ? `确认写入 ${confirmingPendingCount} 条` : "确认并清除"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

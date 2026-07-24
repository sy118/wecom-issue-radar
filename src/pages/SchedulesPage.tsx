import { useEffect, useMemo, useRef, useState } from "react";
import {
  CalendarClock,
  CloudUpload,
  Clock3,
  Edit3,
  Info,
  LoaderCircle,
  Play,
  Plus,
  Power,
  Table2,
  Trash2,
  UsersRound,
} from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import type {
  AppConfig,
  GroupInfo,
  PendingScheduleSync,
  ProcessingOptions,
  ScheduleDefinition,
  ScheduleEvent,
  ScheduleDateMode,
  TaskGroup,
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

const localDate = () => {
  const date = new Date();
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
};

const newSchedule = (config: AppConfig): ScheduleDefinition => ({
  id: `schedule_${Date.now()}`,
  name: "每日群聊导出",
  enabled: true,
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

const processingFrom = (schedule: ScheduleDefinition): ProcessingOptions => ({
  promptId: schedule.promptId,
  smartSheetTemplateId: schedule.smartSheetTemplateId ?? "",
  runOcr: schedule.runOcr,
  runAnalysis: schedule.runAnalysis,
  exportXlsx: schedule.exportXlsx,
  exportMarkdown: schedule.exportMarkdown,
  prepareSmartSheet: schedule.prepareSmartSheet,
});

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

const previewWarningsFrom = (runs: TaskRunResult[]) => {
  const messages = new Set<string>();
  runs.forEach((run) => {
    if (run.smartSheetPreview?.mapping_valid === false) {
      messages.add(run.smartSheetPreview.validation_error || `${run.groupName} 的腾讯文档字段映射无效`);
    }
    if (run.smartSheetPreview?.webhook_configured === false || run.smartSheetPreview?.configured === false) {
      messages.add(`${run.smartSheetPreview.template_name || run.smartSheetTemplateName || run.groupName} 尚未配置写入 Webhook`);
    }
  });
  return [...messages];
};

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
  const confirmationRequestGeneration = useRef(0);

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
      setActivity((current) => ({ ...current, [event.scheduleId]: message }));
      setRunningId("");
      if (event.success !== false) void refreshPendingSyncs(false);
      if (event.success === false) {
        toast.error(`${event.scheduleName}执行失败`, {
          description: message,
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
      cleanups.forEach((cleanup) => cleanup());
    };
  }, []);

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
  const confirmingWarnings = previewWarningsFrom(confirmingRuns);
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
    const normalizedEditor = { ...editor, smartSheetTemplateId };
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

  const updateProcessing = (processing: ProcessingOptions) => updateEditor(processing);
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
        <Button onClick={() => setEditor(newSchedule(config))}><Plus size={16} />新建定时任务</Button>
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
                  <Button variant="ghost" title="编辑" onClick={() => setEditor({ ...schedule, groups: [...schedule.groups], weekdays: [...schedule.weekdays] })}><Edit3 size={15} /></Button>
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
                  smartSheetHint="定时任务仅生成待同步预览，不自动写云端"
                />
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
          <div className="modal-card schedule-sync-modal">
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

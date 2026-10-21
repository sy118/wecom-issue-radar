import { useEffect, useMemo, useRef, useState } from "react";
import { CalendarClock, History, Info, Plus, Power } from "lucide-react";
import { toast } from "sonner";
import { Button } from "../components/ui";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import { smartSheetConfigurationBlockers } from "../lib/smartSheetSync";
import { resolveSmartSheetTemplateId } from "../components/TaskEditor";
import type {
  AppConfig,
  GroupInfo,
  PendingScheduleSync,
  ProcessingOptions,
  ScheduleDefinition,
  ScheduleExecutionHistoryPage,
  TaskRunResult,
} from "../types";
import { ExecutionHistoryModal } from "./schedules/ExecutionHistoryModal";
import { PendingSyncModal } from "./schedules/PendingSyncModal";
import { ScheduleEditorModal } from "./schedules/ScheduleEditorModal";
import { ScheduleList } from "./schedules/ScheduleList";
import {
  HISTORY_PAGE_SIZE,
  automaticSyncWarningSummary,
  canAutoSyncSmartSheet,
  createPendingSyncBatch,
  frozenDefinitionPath,
  frozenSyncDate,
  frozenTemplateId,
  frozenTargetsFrom,
  hardBlockersFrom,
  isCurrentConfirmationRequest,
  isCurrentHistoryRequest,
  newSchedule,
  normalizeScheduleAutoSync,
  orphanPendingSyncGroupsFrom,
  pendingCountFrom,
  pendingRunsFrom,
  scheduleForEditing,
  withoutPendingSyncBatch,
  type PendingSyncBatch,
} from "./schedules/shared";

export * from "./schedules/shared";

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

      <ScheduleList
        sortedSchedules={sortedSchedules}
        orphanPendingGroups={orphanPendingGroups}
        pendingBySchedule={pendingBySchedule}
        activity={activity}
        saving={saving}
        runningId={runningId}
        syncing={syncing}
        refreshingConfirmation={refreshingConfirmation}
        config={config}
        onCreate={() => setEditor(newSchedule(config))}
        onToggle={toggleEnabled}
        onRunNow={runNow}
        onEdit={(schedule) => setEditor(scheduleForEditing(config, schedule))}
        onRemove={remove}
        onOpenPending={openPendingConfirmation}
        onDiscardPending={discardPendingItems}
      />

      {historyOpen && (
        <ExecutionHistoryModal
          sortedSchedules={sortedSchedules}
          historyScheduleId={historyScheduleId}
          historyPage={historyPage}
          historyLoading={historyLoading}
          historyData={historyData}
          historyError={historyError}
          onClose={closeHistory}
          onFilter={filterHistory}
          onRefresh={refreshHistory}
          onChangePage={changeHistoryPage}
          onOpenResult={openHistoryResult}
        />
      )}

      {editor && (
        <ScheduleEditorModal
          editor={editor}
          isExisting={schedules.some((item) => item.id === editor.id)}
          config={config}
          groups={groups}
          loadingGroups={loadingGroups}
          autoSyncAvailable={autoSyncAvailable}
          saving={saving}
          onClose={() => setEditor(null)}
          onUpdate={updateEditor}
          onUpdateProcessing={updateProcessing}
          onUpdateWeekday={updateWeekday}
          onReloadGroups={() => void loadGroups()}
          onSave={() => void saveEditor()}
        />
      )}

      {confirmingScheduleId && confirmingSyncs.length > 0 && (
        <PendingSyncModal
          confirmingScheduleName={confirmingScheduleName}
          confirmingRuns={confirmingRuns}
          confirmingPendingCount={confirmingPendingCount}
          confirmingTargets={confirmingTargets}
          confirmingWarnings={confirmingWarnings}
          confirmingBlockers={confirmingBlockers}
          confirmationRefreshError={confirmationRefreshError}
          refreshingConfirmation={refreshingConfirmation}
          syncing={syncing}
          refreshedConfirmingRuns={refreshedConfirmingRuns}
          onDiscard={() => void discardPendingSync()}
          onClose={closePendingConfirmation}
          onConfirm={() => void confirmPendingSync()}
        />
      )}
    </div>
  );
}

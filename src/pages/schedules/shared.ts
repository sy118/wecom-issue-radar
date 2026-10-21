/**
 * Shared types and pure helpers for the schedules page. Kept JSX-free so the
 * page shell and its extracted list/editor/history components can share them.
 */

import {
  defaultProcessingOptions,
  resolveSmartSheetTemplateId,
} from "../../components/TaskEditor";
import { toUserErrorMessage } from "../../lib/errors";
import type {
  AppConfig,
  PendingScheduleSync,
  ProcessingOptions,
  ScheduleDefinition,
  ScheduleDateMode,
  ScheduleExecutionHistoryItem,
  ScheduleExecutionStatus,
  TaskResult,
  TaskRunResult,
} from "../../types";

export const weekdays = [
  { value: 1, label: "一" },
  { value: 2, label: "二" },
  { value: 3, label: "三" },
  { value: 4, label: "四" },
  { value: 5, label: "五" },
  { value: 6, label: "六" },
  { value: 7, label: "日" },
];

export const HISTORY_PAGE_SIZE = 10;

export const localDate = () => {
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

export const dateModeLabel: Record<ScheduleDateMode, string> = {
  today: "执行当天",
  yesterday: "执行前一天",
  fixed: "固定日期",
};

export const scheduleRangeLabel = (schedule: Pick<ScheduleDefinition, "startTime" | "endTime">) =>
  schedule.endTime < schedule.startTime
    ? `${schedule.startTime}–次日 ${schedule.endTime}`
    : `${schedule.startTime}–${schedule.endTime}`;

export type HistoryOutcomeSource = Pick<ScheduleExecutionHistoryItem, "success" | "result">
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

export const historyDateLabel = (value: string) => {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleString("zh-CN", { hour12: false })
    : "时间未知";
};

export const historyRunStatusLabel = (run: TaskRunResult) => {
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

export const processingFrom = (schedule: ScheduleDefinition): ProcessingOptions => ({
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

export const pendingRunsFrom = (items: PendingScheduleSync[]): TaskRunResult[] => items
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

export const pendingCountFrom = (items: PendingScheduleSync[]) => pendingRunsFrom(items).reduce(
  (total, run) => total + (run.smartSheetPreview?.pending ?? 0),
  0,
);

export const frozenTemplateId = (run: TaskRunResult) =>
  run.smartSheetTemplateId || run.smartSheetPreview?.template_id || "";

export const frozenSyncDate = (run: TaskRunResult) =>
  run.smartSheetDate || run.endDate || run.startDate || "";

export const frozenDefinitionPath = (run: TaskRunResult) => run.definitionPath?.trim() || "";

export const hardBlockersFrom = (runs: TaskRunResult[]) => {
  const messages = new Set<string>();
  runs.forEach((run) => {
    if (!frozenTemplateId(run)) messages.add(`${run.groupName} 缺少冻结的腾讯文档模板 ID`);
    if (!frozenSyncDate(run)) messages.add(`${run.groupName} 缺少同步日期`);
    if (!frozenDefinitionPath(run)) messages.add(`${run.groupName} 缺少冻结的问题定义快照，旧结果只能放弃后重新执行`);
  });
  return [...messages];
};

export const frozenTargetsFrom = (runs: TaskRunResult[]) => {
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

/**
 * TypeScript scheduler equivalent of the previous Rust `scheduler.rs`.
 *
 * Responsibilities: 30 s tick (+ one catch-up pass after a >90 s gap), schedule
 * validation, claim persistence in `schedule-state.json`, execution history
 * paging, pending Tencent-document confirmations, and automatic Smart Sheet
 * sync after a scheduled run.
 *
 * All pure decisions (due/claim/pagination/request assembly/auto-sync result
 * handling) are exported so they can be unit tested without spawning Python.
 */

import type { BootstrapPayload, HistoryQuery, MainContext, TaskRunRequest } from "../../shared/contracts";
import { atomicWriteJson, readJsonFile } from "./files";
import {
  asArray,
  asNonNegativeInteger,
  asNumber,
  asRecord,
  asString,
  errorMessage,
  isRecord,
  trimmedString,
} from "./utils";

export const POLL_INTERVAL_MS = 30_000;
export const CATCH_UP_THRESHOLD_MS = 90_000;
export const MAX_CATCH_UP_MINUTES = 24 * 60;
export const MAX_EXECUTION_HISTORY = 500;
export const MAX_HISTORY_PAGE_SIZE = 50;

const SHANGHAI_TIME_ZONE = "Asia/Shanghai";
const DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;
const CLOCK_PATTERN = /^(\d{2}):(\d{2})$/;

export type ScheduleDateMode = "today" | "yesterday" | "fixed";
export type ScheduleExecutionTrigger = "manual" | "automatic";
export type ScheduleExecutionStatus = "success" | "partial" | "empty" | "failed";

export interface ScheduleTaskGroup {
  id: string;
  name: string;
}

export interface ScheduleDefinition {
  id: string;
  name: string;
  enabled: boolean;
  runAt: string;
  weekdays: number[];
  dateMode: ScheduleDateMode;
  fixedDate: string;
  startTime: string;
  endTime: string;
  groups: ScheduleTaskGroup[];
  promptId: string;
  smartSheetTemplateId: string;
  runOcr: boolean;
  runAnalysis: boolean;
  exportXlsx: boolean;
  exportMarkdown: boolean;
  prepareSmartSheet: boolean;
  autoSyncSmartSheet: boolean;
}

export interface PendingScheduleSync {
  pendingId: string;
  scheduleId: string;
  scheduleName: string;
  createdAt: string;
  result: unknown;
}

export interface ScheduleExecutionHistoryItem {
  executionId: string;
  scheduleId: string;
  scheduleName: string;
  trigger: ScheduleExecutionTrigger;
  startedAt: string;
  finishedAt: string;
  success: boolean;
  status: ScheduleExecutionStatus;
  message: string;
  result?: unknown;
}

export interface ScheduleExecutionHistoryPage {
  items: ScheduleExecutionHistoryItem[];
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
}

export interface ScheduleState {
  lastRuns: Record<string, string>;
  pendingSmartSheetSyncs: PendingScheduleSync[];
  executionHistory: ScheduleExecutionHistoryItem[];
}

export interface ClockSnapshot {
  /** `YYYY-MM-DD` in Asia/Shanghai. */
  date: string;
  /** `HH:MM` in Asia/Shanghai. */
  minute: string;
  /** ISO weekday, Monday = 1 … Sunday = 7. */
  weekday: number;
}

export interface ScheduleCompletion {
  message: string;
  success: boolean;
  status: ScheduleExecutionStatus;
  result?: unknown;
}

export interface AutomaticSyncTarget {
  runIndex: number;
  groupName: string;
  payload: Record<string, unknown> | null;
  payloadError: string | null;
}

export interface InFlightReference {
  scheduleId: string | null;
  ownerLabel: string;
  templateId: string;
}

export interface InFlightGuard {
  release(): void;
}

// ---------------------------------------------------------------------------
// Time helpers (Asia/Shanghai, matching the Rust `chrono_tz::Asia::Shanghai`).
// ---------------------------------------------------------------------------

const shanghaiFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: SHANGHAI_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

export function weekdayFromDate(date: string): number {
  const match = DATE_PATTERN.exec(date.trim());
  if (!match) return 0;
  const [, year, month, day] = match;
  const utcDay = new Date(
    Date.UTC(Number(year), Number(month) - 1, Number(day)),
  ).getUTCDay();
  return utcDay === 0 ? 7 : utcDay;
}

export function shanghaiClock(at: Date): ClockSnapshot {
  const parts: Record<string, string> = {};
  for (const part of shanghaiFormatter.formatToParts(at)) {
    if (part.type !== "literal") parts[part.type] = part.value;
  }
  const date = `${parts.year ?? "1970"}-${parts.month ?? "01"}-${parts.day ?? "01"}`;
  const minute = `${parts.hour ?? "00"}:${parts.minute ?? "00"}`;
  return { date, minute, weekday: weekdayFromDate(date) };
}

export function addDays(date: string, delta: number): string {
  const match = DATE_PATTERN.exec(date.trim());
  if (!match) throw new Error(`日期无效：${date}`);
  const [, year, month, day] = match;
  const value = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
  value.setUTCDate(value.getUTCDate() + delta);
  return `${value.getUTCFullYear()}-${pad2(value.getUTCMonth() + 1)}-${pad2(value.getUTCDate())}`;
}

export function parseIsoDate(value: string): string | null {
  const match = DATE_PATTERN.exec(value.trim());
  if (!match) return null;
  const [, year, month, day] = match;
  const parsed = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
  if (
    parsed.getUTCFullYear() !== Number(year) ||
    parsed.getUTCMonth() !== Number(month) - 1 ||
    parsed.getUTCDate() !== Number(day)
  ) {
    return null;
  }
  return `${year}-${month}-${day}`;
}

export function parseClock(value: string): number | null {
  const match = CLOCK_PATTERN.exec(value.trim());
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (hour > 23 || minute > 59) return null;
  return hour * 60 + minute;
}

/**
 * One catch-up pass: every minute strictly after `lastTick` and before the
 * current minute. Returns no slots when the previous tick is recent (the normal
 * tick already covers the current minute).
 */
export function missedMinuteSlots(lastTick: Date, now: Date): ClockSnapshot[] {
  const gap = now.getTime() - lastTick.getTime();
  if (!Number.isFinite(gap) || gap <= CATCH_UP_THRESHOLD_MS) return [];
  const currentMinute = Math.floor(now.getTime() / 60_000);
  const lastMinute = Math.floor(lastTick.getTime() / 60_000);
  const firstMinute = Math.max(lastMinute + 1, currentMinute - MAX_CATCH_UP_MINUTES + 1);
  const slots: ClockSnapshot[] = [];
  for (let minuteIndex = firstMinute; minuteIndex < currentMinute; minuteIndex += 1) {
    slots.push(shanghaiClock(new Date(minuteIndex * 60_000)));
  }
  return slots;
}

// ---------------------------------------------------------------------------
// Schedule parsing / validation.
// ---------------------------------------------------------------------------

function requireString(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new Error(`定时任务配置无效：${label}必须是字符串`);
  }
  return value;
}

function optionalString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isDateMode(value: unknown): value is ScheduleDateMode {
  return value === "today" || value === "yesterday" || value === "fixed";
}

export function parseSchedule(value: unknown): ScheduleDefinition {
  if (!isRecord(value)) {
    throw new Error("定时任务配置无效：必须是 JSON 对象");
  }
  const rawGroups = value.groups;
  if (!Array.isArray(rawGroups)) {
    throw new Error("定时任务配置无效：groups 必须是数组");
  }
  const groups: ScheduleTaskGroup[] = rawGroups.map((rawGroup) => {
    if (!isRecord(rawGroup)) {
      throw new Error("定时任务配置无效：群聊必须是对象");
    }
    const id = requireString(rawGroup.id, "群聊 ID");
    return { id, name: optionalString(rawGroup.name) || id };
  });
  const rawWeekdays = value.weekdays;
  if (!Array.isArray(rawWeekdays)) {
    throw new Error("定时任务配置无效：weekdays 必须是数组");
  }
  const weekdays = rawWeekdays.map((weekday) => {
    const numeric = asNumber(weekday);
    if (numeric === null) {
      throw new Error("定时任务配置无效：weekdays 必须是数字");
    }
    return Math.trunc(numeric);
  });
  if (!isDateMode(value.dateMode)) {
    throw new Error("定时任务配置无效：dateMode 必须是 today、yesterday 或 fixed");
  }
  return {
    id: requireString(value.id, "任务 ID"),
    name: requireString(value.name, "任务名称"),
    enabled: value.enabled === true,
    runAt: requireString(value.runAt, "执行时间"),
    weekdays,
    dateMode: value.dateMode,
    fixedDate: optionalString(value.fixedDate),
    startTime: requireString(value.startTime, "开始时间"),
    endTime: requireString(value.endTime, "结束时间"),
    groups,
    promptId: optionalString(value.promptId),
    smartSheetTemplateId: optionalString(value.smartSheetTemplateId),
    runOcr: value.runOcr === true,
    runAnalysis: value.runAnalysis === true,
    exportXlsx: value.exportXlsx === true,
    exportMarkdown: value.exportMarkdown === true,
    prepareSmartSheet: value.prepareSmartSheet === true,
    autoSyncSmartSheet: value.autoSyncSmartSheet === true,
  };
}

export function validateSchedule(schedule: ScheduleDefinition): void {
  if (!schedule.id.trim()) {
    throw new Error("定时任务 ID 不能为空");
  }
  if (!schedule.name.trim()) {
    throw new Error(`定时任务 ${schedule.id} 的名称不能为空`);
  }
  if (parseClock(schedule.runAt) === null) {
    throw new Error(`任务“${schedule.name}”的执行时间无效`);
  }
  if (
    schedule.weekdays.length === 0 ||
    schedule.weekdays.some((weekday) => weekday < 1 || weekday > 7)
  ) {
    throw new Error(`任务“${schedule.name}”的执行日无效`);
  }
  if (parseClock(schedule.startTime) === null) {
    throw new Error(`任务“${schedule.name}”的开始时间无效`);
  }
  if (parseClock(schedule.endTime) === null) {
    throw new Error(`任务“${schedule.name}”的结束时间无效`);
  }
  if (schedule.dateMode === "fixed" && parseIsoDate(schedule.fixedDate) === null) {
    throw new Error(`任务“${schedule.name}”的固定日期无效`);
  }
  if (
    schedule.groups.length === 0 ||
    schedule.groups.some((group) => !group.id.trim())
  ) {
    throw new Error(`任务“${schedule.name}”至少需要一个有效群聊`);
  }
  if (schedule.autoSyncSmartSheet && !schedule.runAnalysis) {
    throw new Error(`任务“${schedule.name}”自动同步 Smart Sheet 前必须启用大模型分析`);
  }
  if (schedule.autoSyncSmartSheet && !schedule.prepareSmartSheet) {
    throw new Error(`任务“${schedule.name}”自动同步 Smart Sheet 前必须启用 Smart Sheet`);
  }
  if (schedule.autoSyncSmartSheet && !schedule.smartSheetTemplateId.trim()) {
    throw new Error(`任务“${schedule.name}”自动同步 Smart Sheet 前必须冻结腾讯文档模板`);
  }
  if (schedule.prepareSmartSheet && !schedule.runAnalysis) {
    throw new Error(`任务“${schedule.name}”准备 Smart Sheet 前必须启用大模型分析`);
  }
  if (schedule.prepareSmartSheet && !schedule.smartSheetTemplateId.trim()) {
    throw new Error(`任务“${schedule.name}”准备 Smart Sheet 前必须冻结腾讯文档模板`);
  }
}

export function isScheduleDue(
  schedule: ScheduleDefinition,
  weekday: number,
  minute: string,
): boolean {
  return (
    schedule.enabled &&
    schedule.runAt === minute &&
    schedule.weekdays.includes(weekday)
  );
}

export function resolveExportDate(schedule: ScheduleDefinition, today: string): string {
  switch (schedule.dateMode) {
    case "today":
      return today;
    case "yesterday":
      return addDays(today, -1);
    case "fixed": {
      const fixed = parseIsoDate(schedule.fixedDate);
      if (!fixed) throw new Error(`固定日期无效：${schedule.fixedDate}`);
      return fixed;
    }
  }
}

export function resolveExportRange(
  schedule: ScheduleDefinition,
  today: string,
): { startDate: string; endDate: string } {
  const startDate = resolveExportDate(schedule, today);
  const startClock = parseClock(schedule.startTime);
  if (startClock === null) {
    throw new Error(`开始时间无效：${schedule.startTime}`);
  }
  const endClock = parseClock(schedule.endTime);
  if (endClock === null) {
    throw new Error(`结束时间无效：${schedule.endTime}`);
  }
  const endDate = endClock < startClock ? addDays(startDate, 1) : startDate;
  return { startDate, endDate };
}

export function buildWorkerRunRequest(
  schedule: ScheduleDefinition,
  startDate: string,
  endDate: string,
): Record<string, unknown> {
  return {
    date: startDate,
    startDate,
    endDate,
    startTime: schedule.startTime,
    endTime: schedule.endTime,
    groups: schedule.groups,
    promptId: schedule.promptId,
    smartSheetTemplateId: schedule.smartSheetTemplateId,
    runOcr: schedule.runOcr,
    runAnalysis: schedule.runAnalysis,
    exportXlsx: schedule.exportXlsx,
    exportMarkdown: schedule.exportMarkdown,
    prepareSmartSheet: schedule.prepareSmartSheet,
  };
}

// ---------------------------------------------------------------------------
// Config projections.
// ---------------------------------------------------------------------------

export function scheduleIdsFromConfig(config: Record<string, unknown>): Set<string> {
  const ids = new Set<string>();
  for (const entry of asArray(config.schedules)) {
    const id = trimmedString(asRecord(entry)?.id);
    if (id) ids.add(id);
  }
  return ids;
}

export function configuredTemplateIds(config: Record<string, unknown>): Set<string> {
  const ids = new Set<string>();
  const templates = asArray(asRecord(config.smart_sheet)?.templates);
  for (const template of templates) {
    const id = trimmedString(asRecord(template)?.id);
    if (id) ids.add(id);
  }
  return ids;
}

export function protectedScheduleIds(
  pendingSyncs: PendingScheduleSync[],
  references: InFlightReference[],
): Set<string> {
  const ids = new Set<string>();
  for (const pending of pendingSyncs) ids.add(pending.scheduleId);
  for (const reference of references) {
    if (reference.scheduleId) ids.add(reference.scheduleId);
  }
  return ids;
}

export function protectedScheduleDeletions(
  currentIds: Set<string>,
  incomingIds: Set<string>,
  protectedIds: Set<string>,
): Set<string> {
  const blocked = new Set<string>();
  for (const id of currentIds) {
    if (!incomingIds.has(id) && protectedIds.has(id)) blocked.add(id);
  }
  return blocked;
}

export function validateAutoSyncTemplateReferences(
  schedules: ScheduleDefinition[],
  config: Record<string, unknown>,
): void {
  const templateIds = configuredTemplateIds(config);
  for (const schedule of schedules) {
    if (!schedule.autoSyncSmartSheet) continue;
    const templateId = schedule.smartSheetTemplateId.trim();
    if (!templateIds.has(templateId)) {
      throw new Error(
        `任务“${schedule.name}”自动同步引用了不存在的腾讯文档模板“${templateId}”`,
      );
    }
  }
}

// ---------------------------------------------------------------------------
// Claim / history / pending-sync state.
// ---------------------------------------------------------------------------

export function createEmptyScheduleState(): ScheduleState {
  return { lastRuns: {}, pendingSmartSheetSyncs: [], executionHistory: [] };
}

export function normalizeScheduleState(value: unknown): ScheduleState {
  if (value === null || value === undefined) return createEmptyScheduleState();
  if (!isRecord(value)) throw new Error("定时任务状态文件无效");
  const lastRuns: Record<string, string> = {};
  if (isRecord(value.lastRuns)) {
    for (const [key, slot] of Object.entries(value.lastRuns)) {
      if (typeof slot === "string") lastRuns[key] = slot;
    }
  }
  return {
    lastRuns,
    pendingSmartSheetSyncs: asArray(value.pendingSmartSheetSyncs).filter(
      isRecord,
    ) as unknown as PendingScheduleSync[],
    executionHistory: asArray(value.executionHistory).filter(
      isRecord,
    ) as unknown as ScheduleExecutionHistoryItem[],
  };
}

/**
 * Claims a single automatic execution per schedule and date. The minute is
 * stored for diagnostics only; the date alone prevents duplicate runs.
 */
export function claimScheduleRun(
  state: ScheduleState,
  scheduleId: string,
  date: string,
  minute: string,
): boolean {
  const last = state.lastRuns[scheduleId];
  if (typeof last === "string") {
    const [runDate] = last.split(" ");
    if (runDate === date) return false;
  }
  state.lastRuns[scheduleId] = `${date} ${minute}`;
  return true;
}

export function validateHistoryPagination(page: number, pageSize: number): void {
  if (!Number.isInteger(page) || page === 0) {
    throw new Error("执行记录页码必须从 1 开始");
  }
  if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > MAX_HISTORY_PAGE_SIZE) {
    throw new Error(`执行记录每页数量必须在 1 到 ${MAX_HISTORY_PAGE_SIZE} 之间`);
  }
}

export function paginateExecutionHistory(
  state: ScheduleState,
  page: number,
  pageSize: number,
  scheduleId?: string | null,
): ScheduleExecutionHistoryPage {
  validateHistoryPagination(page, pageSize);
  const filterId = scheduleId?.trim() || null;
  const matches = state.executionHistory
    .filter((item) => (filterId ? item.scheduleId === filterId : true))
    .map((item) => ({ ...item }));
  matches.sort((left, right) => {
    const byFinished = String(right.finishedAt ?? "").localeCompare(
      String(left.finishedAt ?? ""),
    );
    if (byFinished !== 0) return byFinished;
    return String(right.executionId ?? "").localeCompare(String(left.executionId ?? ""));
  });
  const total = matches.length;
  const totalPages = total === 0 ? 0 : Math.ceil(total / pageSize);
  const start = (page - 1) * pageSize;
  return {
    items: matches.slice(start, start + pageSize),
    page,
    pageSize,
    total,
    totalPages,
  };
}

export interface ExecutionHistoryInput {
  schedule: ScheduleDefinition;
  trigger: ScheduleExecutionTrigger;
  startedAt: Date;
  finishedAt: Date;
  success: boolean;
  status: ScheduleExecutionStatus;
  message: string;
  result?: unknown;
}

export function pushExecutionHistoryItem(
  state: ScheduleState,
  input: ExecutionHistoryInput,
): ScheduleExecutionHistoryItem {
  const baseId = `${input.schedule.id}:${input.startedAt.getTime()}`;
  let executionId = baseId;
  let suffix = 2;
  while (state.executionHistory.some((item) => item.executionId === executionId)) {
    executionId = `${baseId}:${suffix}`;
    suffix += 1;
  }
  const item: ScheduleExecutionHistoryItem = {
    executionId,
    scheduleId: input.schedule.id,
    scheduleName: input.schedule.name,
    trigger: input.trigger,
    startedAt: input.startedAt.toISOString(),
    finishedAt: input.finishedAt.toISOString(),
    success: input.success,
    status: input.status,
    message: input.message,
    ...(input.result === undefined ? {} : { result: input.result }),
  };
  state.executionHistory.push(item);
  state.executionHistory.sort((left, right) => {
    const byFinished = String(left.finishedAt).localeCompare(String(right.finishedAt));
    if (byFinished !== 0) return byFinished;
    return String(left.executionId).localeCompare(String(right.executionId));
  });
  const excess = state.executionHistory.length - MAX_EXECUTION_HISTORY;
  if (excess > 0) {
    state.executionHistory.splice(0, excess);
  }
  return item;
}

export function pushPendingSyncItem(
  state: ScheduleState,
  schedule: ScheduleDefinition,
  result: unknown,
  now: Date,
): PendingScheduleSync {
  const baseId = `${schedule.id}:${now.getTime()}`;
  let pendingId = baseId;
  let suffix = 2;
  while (
    state.pendingSmartSheetSyncs.some((pending) => pending.pendingId === pendingId)
  ) {
    pendingId = `${baseId}:${suffix}`;
    suffix += 1;
  }
  const pending: PendingScheduleSync = {
    pendingId,
    scheduleId: schedule.id,
    scheduleName: schedule.name,
    createdAt: now.toISOString(),
    result,
  };
  state.pendingSmartSheetSyncs.push(pending);
  return pending;
}

export function clearPendingInState(
  state: ScheduleState,
  pendingIds: Set<string>,
): boolean {
  const previousLength = state.pendingSmartSheetSyncs.length;
  state.pendingSmartSheetSyncs = state.pendingSmartSheetSyncs.filter(
    (pending) => !pendingIds.has(pending.pendingId),
  );
  return state.pendingSmartSheetSyncs.length !== previousLength;
}

// ---------------------------------------------------------------------------
// Execution result handling.
// ---------------------------------------------------------------------------

export function executionStatusFromWorkerResult(result: unknown): ScheduleExecutionStatus {
  const status = isRecord(result) ? asString(result.status) : null;
  switch (status) {
    case "partial":
      return "partial";
    case "empty":
      return "empty";
    case "failed":
      return "failed";
    case "success":
    case null:
      return "success";
    default:
      return "failed";
  }
}

export function workerCompletion(result: unknown): {
  status: ScheduleExecutionStatus;
  success: boolean;
  message: string;
} {
  const status = executionStatusFromWorkerResult(result);
  switch (status) {
    case "success":
      return { status, success: true, message: "任务执行完成" };
    case "partial":
      return { status, success: true, message: "任务部分完成，请查看失败群聊" };
    case "empty":
      return {
        status,
        success: true,
        message: "任务执行完成，所选群聊均无可分析记录",
      };
    case "failed":
      return { status, success: false, message: "任务执行失败，请查看执行结果" };
  }
}

export function resultHasPendingSmartSheetRecords(result: unknown): boolean {
  const previewHasPending = (value: unknown): boolean => {
    const preview = asRecord(asRecord(value)?.smartSheetPreview);
    return (asNonNegativeInteger(preview?.pending) ?? 0) > 0;
  };
  if (previewHasPending(result)) return true;
  const runs = asArray(asRecord(result)?.runs);
  return runs.some(previewHasPending);
}

export function shouldPersistPendingSync(
  schedule: ScheduleDefinition,
  result: unknown,
): boolean {
  return (
    schedule.prepareSmartSheet &&
    !schedule.autoSyncSmartSheet &&
    resultHasPendingSmartSheetRecords(result)
  );
}

export function automaticSyncTargets(
  result: unknown,
  configPath: string,
): AutomaticSyncTarget[] {
  const targets: AutomaticSyncTarget[] = [];
  const runs = asArray(asRecord(result)?.runs);
  runs.forEach((run, runIndex) => {
    if (!isRecord(run)) return;
    const status = asString(run.status);
    const preview = asRecord(run.smartSheetPreview);
    const pending = asNonNegativeInteger(preview?.pending) ?? 0;
    if (!(status === "success" || status === null) || pending <= 0) return;
    const groupName =
      trimmedString(run.groupName) ?? trimmedString(run.groupId) ?? "未命名群聊";
    try {
      targets.push({
        runIndex,
        groupName,
        payload: buildAutomaticSyncPayload(run, configPath),
        payloadError: null,
      });
    } catch (error) {
      targets.push({
        runIndex,
        groupName,
        payload: null,
        payloadError: errorMessage(error),
      });
    }
  });
  return targets;
}

export function buildAutomaticSyncPayload(
  run: Record<string, unknown>,
  configPath: string,
): Record<string, unknown> {
  const preview = asRecord(run.smartSheetPreview);
  if (!preview) throw new Error("缺少腾讯文档预览");
  const requiredRun = (key: string, label: string): string => {
    const value = trimmedString(run[key]);
    if (!value) throw new Error(`缺少${label}`);
    return value;
  };
  const requiredPreview = (key: string, label: string): string => {
    const value = trimmedString(preview[key]);
    if (!value) throw new Error(`缺少${label}`);
    return value;
  };
  const date =
    ["smartSheetDate", "endDate", "startDate"]
      .map((key) => trimmedString(run[key]))
      .find((value) => value !== null) ?? null;
  if (!date) throw new Error("缺少腾讯文档同步日期");
  const templateId =
    trimmedString(run.smartSheetTemplateId) ?? trimmedString(preview.template_id);
  if (!templateId) throw new Error("缺少冻结的腾讯文档模板 ID");
  return {
    configPath,
    dayDir: requiredRun("dayDir", "本地结果目录"),
    date,
    templateId,
    uploadImages: true,
    // Automatic runs are unattended: write text and any available images, then
    // preserve an explicit warning for images that were not available.
    allowMissingImages: true,
    definitionPath: requiredRun("definitionPath", "冻结的问题定义快照"),
    expectedTemplateRevision: requiredPreview("template_revision", "模板 revision"),
    expectedDocumentRevision: requiredPreview(
      "document_revision",
      "问题快照 revision",
    ),
  };
}

export function applyAutomaticSyncSuccess(
  result: unknown,
  runIndex: number,
  response: unknown,
): unknown {
  const root = asRecord(result);
  if (!root) return result;
  const runs = Array.isArray(root.runs) ? root.runs : null;
  if (!runs) return result;
  const run = asRecord(runs[runIndex]);
  if (!run) return result;
  const responseRecord = asRecord(response) ?? {};
  const preview = asRecord(run.smartSheetPreview);
  const imageIntegrity = asRecord(preview?.image_integrity);
  const imagesMapped = imageIntegrity?.mapped === true;
  const previewMissing = asNonNegativeInteger(imageIntegrity?.missing) ?? 0;
  let missingImages = 0;
  if (imagesMapped) {
    const expected = asNonNegativeInteger(imageIntegrity?.expected);
    const actual = asNonNegativeInteger(responseRecord.image_count);
    missingImages =
      expected !== null && actual !== null
        ? Math.max(0, expected - actual)
        : previewMissing;
  }
  const pending = asNonNegativeInteger(preview?.pending) ?? 0;
  const synced = asNonNegativeInteger(responseRecord.synced) ?? pending;
  if (preview) {
    const previous = asNonNegativeInteger(preview.already_synced) ?? 0;
    preview.pending = 0;
    preview.already_synced =
      asNonNegativeInteger(responseRecord.total) ?? previous + synced;
  }
  const syncResult: Record<string, unknown> = {
    mode: "automatic",
    status: "success",
    synced,
  };
  if (missingImages > 0) {
    syncResult.missingImages = missingImages;
    syncResult.warning = `缺少 ${missingImages} 张图片，文字和当前可用图片已写入；缺失图片未同步，请在腾讯文档中核对并手动补图`;
  }
  run.smartSheetSync = syncResult;
  recomputeResultSummary(root);
  return root;
}

export function applyAutomaticSyncFailure(
  result: unknown,
  runIndex: number,
  groupName: string,
  error: string,
): unknown {
  const root = asRecord(result);
  if (!root) return result;
  const runs = Array.isArray(root.runs) ? root.runs : null;
  if (!runs) return result;
  const run = asRecord(runs[runIndex]);
  if (!run) return result;
  const message = `群聊“${groupName}”腾讯文档自动同步失败：${error.trim()}`;
  run.status = "failed";
  run.error = message;
  run.smartSheetSync = { mode: "automatic", status: "failed", error: message };
  recomputeResultSummary(root);
  return root;
}

export function recomputeResultSummary(result: unknown): void {
  const root = asRecord(result);
  if (!root) return;
  const runs = Array.isArray(root.runs) ? root.runs.filter(isRecord) : [];
  if (runs.length === 0) return;
  let successCount = 0;
  let emptyCount = 0;
  let failedCount = 0;
  for (const run of runs) {
    switch (asString(run.status)) {
      case "empty":
        emptyCount += 1;
        break;
      case "failed":
        failedCount += 1;
        break;
      case "success":
      case null:
        successCount += 1;
        break;
      default:
        failedCount += 1;
        break;
    }
  }
  const totalCount = runs.length;
  const status =
    failedCount === totalCount
      ? "failed"
      : failedCount > 0
        ? "partial"
        : successCount > 0
          ? "success"
          : "empty";
  root.status = status;
  root.totalCount = totalCount;
  root.successCount = successCount;
  root.emptyCount = emptyCount;
  root.failedCount = failedCount;
  if (runs.length === 1) {
    const run = runs[0];
    if (run.error !== undefined) root.error = run.error;
    if (run.smartSheetPreview !== undefined) {
      root.smartSheetPreview = run.smartSheetPreview;
    }
    if (run.smartSheetSync !== undefined) root.smartSheetSync = run.smartSheetSync;
  }
}

export function automaticSyncCompletion(result: unknown): ScheduleCompletion {
  const root = asRecord(result) ?? {};
  const runs = asArray(root.runs).filter(isRecord);
  const failureGroups = runs
    .filter((run) => asString(asRecord(run.smartSheetSync)?.status) === "failed")
    .map((run) => asString(run.groupName) ?? asString(run.groupId) ?? "未命名群聊");
  const successfulSyncCount = runs.filter(
    (run) => asString(asRecord(run.smartSheetSync)?.status) === "success",
  ).length;
  let warningGroupCount = 0;
  let missingImageCount = 0;
  for (const run of runs) {
    const sync = asRecord(run.smartSheetSync);
    const warning = trimmedString(sync?.warning);
    if (!warning) continue;
    warningGroupCount += 1;
    missingImageCount += asNonNegativeInteger(sync?.missingImages) ?? 0;
  }
  const base = workerCompletion(root);
  if (failureGroups.length === 0) {
    let completionMessage =
      successfulSyncCount > 0 ? `${base.message}，腾讯文档已自动同步` : base.message;
    if (warningGroupCount > 0) {
      const warningDetail =
        missingImageCount > 0
          ? `${warningGroupCount} 个群共缺少 ${missingImageCount} 张图片，文字和当前可用图片已写入`
          : `${warningGroupCount} 个群存在图片完整性警告`;
      completionMessage += `；${warningDetail}；缺失图片未同步，请在腾讯文档中核对并手动补图`;
    }
    return {
      message: completionMessage,
      success: base.success,
      status: base.status,
      result: root,
    };
  }
  return {
    message: `腾讯文档自动同步失败：${failureGroups.join("、")}；请查看各群执行结果`,
    success: false,
    status: base.status,
    result: root,
  };
}

// ---------------------------------------------------------------------------
// Task-reference guards for template deletion.
// ---------------------------------------------------------------------------

interface PendingTemplateReference {
  scheduleName: string;
  templateId: string;
  templateName: string;
}

export function pendingTemplateReferences(
  pendingSyncs: PendingScheduleSync[],
): PendingTemplateReference[] {
  const seen = new Set<string>();
  const references: PendingTemplateReference[] = [];
  for (const pending of pendingSyncs) {
    const result = asRecord(pending.result);
    const runs = asArray(result?.runs).filter(isRecord);
    const candidates = runs.length > 0 ? runs : result ? [result] : [];
    for (const run of candidates) {
      const preview = asRecord(run.smartSheetPreview);
      if ((asNonNegativeInteger(preview?.pending) ?? 0) <= 0) continue;
      const templateId =
        trimmedString(run.smartSheetTemplateId) ?? trimmedString(preview?.template_id);
      if (!templateId || seen.has(`${pending.pendingId}:${templateId}`)) continue;
      seen.add(`${pending.pendingId}:${templateId}`);
      references.push({
        scheduleName: pending.scheduleName,
        templateId,
        templateName:
          trimmedString(run.smartSheetTemplateName) ??
          trimmedString(preview?.template_name) ??
          "",
      });
    }
  }
  return references;
}

export function missingPendingTemplateReference(
  config: Record<string, unknown>,
  pendingSyncs: PendingScheduleSync[],
): PendingTemplateReference | null {
  const available = configuredTemplateIds(config);
  return (
    pendingTemplateReferences(pendingSyncs).find(
      (reference) => !available.has(reference.templateId),
    ) ?? null
  );
}

export function missingInFlightTemplateReference(
  config: Record<string, unknown>,
  references: InFlightReference[],
): InFlightReference | null {
  const available = configuredTemplateIds(config);
  return (
    references.find(
      (reference) =>
        reference.templateId !== "" && !available.has(reference.templateId),
    ) ?? null
  );
}

// ---------------------------------------------------------------------------
// In-flight registry.
// ---------------------------------------------------------------------------

function referenceKey(reference: InFlightReference): string {
  return JSON.stringify([
    reference.scheduleId ?? "",
    reference.ownerLabel,
    reference.templateId,
  ]);
}

export class InFlightRegistry {
  private readonly entries = new Map<
    string,
    { reference: InFlightReference; count: number }
  >();

  register(reference: InFlightReference): InFlightGuard {
    const key = referenceKey(reference);
    const existing = this.entries.get(key);
    if (existing) {
      existing.count += 1;
    } else {
      this.entries.set(key, { reference, count: 1 });
    }
    let released = false;
    return {
      release: () => {
        if (released) return;
        released = true;
        const entry = this.entries.get(key);
        if (!entry) return;
        if (entry.count > 1) {
          entry.count -= 1;
        } else {
          this.entries.delete(key);
        }
      },
    };
  }

  snapshot(): InFlightReference[] {
    return [...this.entries.values()].map((entry) => entry.reference);
  }
}

// ---------------------------------------------------------------------------
// Persistent state store.
// ---------------------------------------------------------------------------

export class ScheduleStateStore {
  private queue: Promise<unknown> = Promise.resolve();

  constructor(private readonly statePath: string) {}

  async read(): Promise<ScheduleState> {
    const value = await readJsonFile<unknown>(this.statePath);
    return normalizeScheduleState(value);
  }

  async claimRun(scheduleId: string, date: string, minute: string): Promise<boolean> {
    let claimed = false;
    await this.update((state) => {
      claimed = claimScheduleRun(state, scheduleId, date, minute);
      return claimed;
    });
    return claimed;
  }

  async appendExecutionHistory(input: ExecutionHistoryInput): Promise<void> {
    await this.update((state) => {
      pushExecutionHistoryItem(state, input);
      return true;
    });
  }

  async appendPendingSync(
    schedule: ScheduleDefinition,
    result: unknown,
    now: Date,
  ): Promise<void> {
    await this.update((state) => {
      pushPendingSyncItem(state, schedule, result, now);
      return true;
    });
  }

  async listPendingSyncs(): Promise<PendingScheduleSync[]> {
    return (await this.read()).pendingSmartSheetSyncs;
  }

  async clearPendingSyncs(pendingIds: string[]): Promise<void> {
    if (pendingIds.length === 0) return;
    const ids = new Set(pendingIds);
    await this.update((state) => clearPendingInState(state, ids));
  }

  private update(mutator: (state: ScheduleState) => boolean): Promise<ScheduleState> {
    return this.serialize(async () => {
      const state = await this.read();
      const changed = mutator(state);
      if (changed) {
        await atomicWriteJson(this.statePath, state);
      }
      return state;
    });
  }

  private serialize<T>(task: () => Promise<T>): Promise<T> {
    const run = this.queue.then(task, task);
    this.queue = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }
}

// ---------------------------------------------------------------------------
// Scheduler service.
// ---------------------------------------------------------------------------

export interface ScheduleConfigProvider {
  load(): Promise<Record<string, unknown>>;
  getConfig(): Record<string, unknown> | null;
  saveConfig(config: Record<string, unknown>): Promise<BootstrapPayload>;
  saveSchedules(
    config: Record<string, unknown>,
    schedules: unknown[],
  ): Promise<BootstrapPayload>;
}

export interface ScheduleWorkerRunner {
  runTask(request: TaskRunRequest): Promise<unknown>;
  runWorkerAction(action: string, payload?: Record<string, unknown>): Promise<unknown>;
}

export interface SchedulerServiceContext {
  configPath: string;
  scheduleStatePath: string;
  publish: MainContext["publish"];
}

export interface SchedulerServiceOptions {
  pollIntervalMs?: number;
  now?: () => Date;
  logger?: (message: string) => void;
}

export class SchedulerService {
  private readonly store: ScheduleStateStore;
  private readonly inFlight = new InFlightRegistry();
  private readonly pollIntervalMs: number;
  private readonly now: () => Date;
  private readonly logger: (message: string) => void;
  private running = false;
  private loopPromise: Promise<void> | null = null;
  private timer: NodeJS.Timeout | null = null;
  private wake: (() => void) | null = null;
  private lastTick: Date | null = null;

  constructor(
    private readonly config: ScheduleConfigProvider,
    private readonly worker: ScheduleWorkerRunner,
    private readonly ctx: SchedulerServiceContext,
    options: SchedulerServiceOptions = {},
  ) {
    this.store = new ScheduleStateStore(ctx.scheduleStatePath);
    this.pollIntervalMs = options.pollIntervalMs ?? POLL_INTERVAL_MS;
    this.now = options.now ?? (() => new Date());
    this.logger = options.logger ?? ((message) => console.error(message));
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.loopPromise = this.runLoop();
  }

  async stop(): Promise<void> {
    if (!this.running && !this.loopPromise) return;
    this.running = false;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.wake?.();
    await this.loopPromise?.catch(() => undefined);
    this.loopPromise = null;
  }

  async runTickOnce(): Promise<void> {
    const now = this.now();
    const current = shanghaiClock(now);
    const previousTick = this.lastTick;
    // Record the tick even when the config cannot be loaded: a long outage must
    // not turn the next successful tick into a huge catch-up pass.
    this.lastTick = now;
    let config: Record<string, unknown>;
    try {
      config = await this.config.load();
    } catch (error) {
      this.logger(`读取定时任务配置失败：${errorMessage(error)}`);
      return;
    }
    const schedules = this.validSchedules(config);
    const slots: ClockSnapshot[] = [];
    if (previousTick) {
      slots.push(...missedMinuteSlots(previousTick, now));
    }
    slots.push(current);
    for (const slot of slots) {
      for (const schedule of schedules) {
        if (!isScheduleDue(schedule, slot.weekday, slot.minute)) continue;
        await this.startAutomaticRun(schedule, slot);
      }
    }
  }

  async listSchedules(): Promise<unknown[]> {
    return this.listSchedulesRaw();
  }

  async saveSchedules(schedules: unknown[]): Promise<unknown> {
    const parsed: ScheduleDefinition[] = [];
    const incomingIds = new Set<string>();
    for (const value of schedules) {
      const schedule = parseSchedule(value);
      validateSchedule(schedule);
      if (incomingIds.has(schedule.id)) {
        throw new Error(`定时任务 ID 重复：${schedule.id}`);
      }
      incomingIds.add(schedule.id);
      parsed.push(schedule);
    }
    const current = await this.config.load();
    validateAutoSyncTemplateReferences(parsed, current);
    const pendingSyncs = await this.store.listPendingSyncs();
    const references = this.inFlight.snapshot();
    const currentIds = scheduleIdsFromConfig(current);
    const protectedIds = protectedScheduleIds(pendingSyncs, references);
    const blockedIds = protectedScheduleDeletions(currentIds, incomingIds, protectedIds);
    for (const reference of references) {
      if (reference.scheduleId && blockedIds.has(reference.scheduleId)) {
        throw new Error(`${reference.ownerLabel}，请等待任务完成后再删除`);
      }
    }
    for (const pending of pendingSyncs) {
      if (blockedIds.has(pending.scheduleId)) {
        throw new Error(
          `定时任务“${pending.scheduleName}”仍有腾讯文档待确认结果，请先同步或放弃后再删除`,
        );
      }
    }
    const saved = await this.config.saveSchedules(current, schedules);
    const savedSchedules = asArray(saved.config.schedules);
    if (schedules.length > 0 && !sameScheduleIdentity(savedSchedules, schedules)) {
      throw new Error("定时任务保存失败：处理引擎返回的配置与提交不一致，请重试");
    }
    return savedSchedules;
  }

  async runScheduleNow(scheduleId: string): Promise<void> {
    const schedule = await this.findSchedule(scheduleId);
    const guard = this.registerScheduleReference(schedule);
    void this.executeSchedule(schedule, "manual", null).finally(() => guard.release());
  }

  async runTask(request: TaskRunRequest): Promise<unknown> {
    const templateId = trimmedString(request["smartSheetTemplateId"]) ?? "";
    if (request["prepareSmartSheet"] === true && !templateId) {
      throw new Error("准备腾讯文档同步前必须冻结模板 ID");
    }
    if (templateId) {
      const config = await this.config.load();
      if (!configuredTemplateIds(config).has(templateId)) {
        throw new Error(`腾讯文档模板“${templateId}”不存在，无法启动任务`);
      }
    }
    const guard = this.inFlight.register({
      scheduleId: null,
      ownerLabel: "正在运行的手动任务",
      templateId,
    });
    try {
      return await this.worker.runTask(request);
    } finally {
      guard.release();
    }
  }

  async saveConfig(config: Record<string, unknown>): Promise<BootstrapPayload> {
    if (!isRecord(config)) {
      throw new Error("配置数据无效：必须是 JSON 对象");
    }
    const pendingSyncs = await this.store.listPendingSyncs();
    const missingPending = missingPendingTemplateReference(config, pendingSyncs);
    if (missingPending) {
      const templateLabel = missingPending.templateName
        ? `${missingPending.templateName}（${missingPending.templateId}）`
        : missingPending.templateId;
      throw new Error(
        `腾讯文档模板“${templateLabel}”仍被定时任务“${missingPending.scheduleName}”的待确认结果使用，请先完成同步或放弃该结果后再删除模板`,
      );
    }
    const missingInFlight = missingInFlightTemplateReference(
      config,
      this.inFlight.snapshot(),
    );
    if (missingInFlight) {
      throw new Error(
        `腾讯文档模板“${missingInFlight.templateId}”仍被${missingInFlight.ownerLabel}使用，请等待任务完成后再删除模板`,
      );
    }
    return this.config.saveConfig(config);
  }

  async listScheduleExecutionHistory(
    query: HistoryQuery,
  ): Promise<ScheduleExecutionHistoryPage> {
    const state = await this.store.read();
    return paginateExecutionHistory(
      state,
      query.page,
      query.pageSize,
      query.scheduleId ?? null,
    );
  }

  async listPendingSmartSheetSyncs(): Promise<PendingScheduleSync[]> {
    return this.store.listPendingSyncs();
  }

  async clearPendingSmartSheetSyncs(pendingIds: string[]): Promise<void> {
    await this.store.clearPendingSyncs(pendingIds);
  }

  // -- internals ------------------------------------------------------------

  private validSchedules(config: Record<string, unknown>): ScheduleDefinition[] {
    const valid: ScheduleDefinition[] = [];
    for (const value of asArray(config.schedules)) {
      try {
        const schedule = parseSchedule(value);
        validateSchedule(schedule);
        valid.push(schedule);
      } catch (error) {
        this.logger(`已跳过无效的定时任务：${errorMessage(error)}`);
      }
    }
    return valid;
  }

  private async listSchedulesRaw(): Promise<unknown[]> {
    const config = await this.config.load();
    const schedules = config.schedules;
    return Array.isArray(schedules) ? schedules : [];
  }

  private async findSchedule(scheduleId: string): Promise<ScheduleDefinition> {
    const schedules = await this.listSchedulesRaw();
    const value = schedules.find(
      (candidate) => trimmedString(asRecord(candidate)?.id) === scheduleId,
    );
    if (!value) {
      throw new Error(`找不到定时任务：${scheduleId}`);
    }
    const schedule = parseSchedule(value);
    validateSchedule(schedule);
    return schedule;
  }

  private registerScheduleReference(schedule: ScheduleDefinition): InFlightGuard {
    return this.inFlight.register({
      scheduleId: schedule.id,
      ownerLabel: `正在运行的定时任务“${schedule.name}”`,
      templateId: schedule.smartSheetTemplateId.trim(),
    });
  }

  private async startAutomaticRun(
    schedule: ScheduleDefinition,
    slot: ClockSnapshot,
  ): Promise<void> {
    let guard: InFlightGuard;
    try {
      guard = this.registerScheduleReference(schedule);
    } catch (error) {
      this.logger(`定时任务启动前复核失败：${errorMessage(error)}`);
      return;
    }
    try {
      const latest = await this.findSchedule(schedule.id);
      if (!isScheduleDue(latest, slot.weekday, slot.minute)) {
        guard.release();
        return;
      }
      const claimed = await this.store.claimRun(latest.id, slot.date, slot.minute);
      if (!claimed) {
        guard.release();
        return;
      }
      void this.executeSchedule(latest, "automatic", slot.date).finally(() =>
        guard.release(),
      );
    } catch (error) {
      guard.release();
      this.logger(`定时任务启动前复核失败：${errorMessage(error)}`);
    }
  }

  private async executeSchedule(
    schedule: ScheduleDefinition,
    trigger: ScheduleExecutionTrigger,
    referenceDate: string | null,
  ): Promise<void> {
    const startedAt = this.now();
    const today = referenceDate ?? shanghaiClock(startedAt).date;
    let range: { startDate: string; endDate: string };
    try {
      range = resolveExportRange(schedule, today);
    } catch (error) {
      await this.complete(schedule, trigger, startedAt, {
        message: errorMessage(error),
        success: false,
        status: "failed",
      });
      return;
    }
    const rangeLabel =
      range.startDate === range.endDate
        ? `${range.startDate} ${schedule.startTime}–${schedule.endTime}`
        : `${range.startDate} ${schedule.startTime}–${range.endDate} ${schedule.endTime}`;
    this.emitProgress(
      schedule,
      `正在导出 ${rangeLabel} 的 ${schedule.groups.length} 个群聊…`,
    );
    const request = buildWorkerRunRequest(schedule, range.startDate, range.endDate);
    let result: unknown;
    try {
      result = await this.worker.runWorkerAction("run", {
        configPath: this.ctx.configPath,
        request,
      });
    } catch (error) {
      await this.complete(schedule, trigger, startedAt, {
        message: errorMessage(error),
        success: false,
        status: "failed",
      });
      return;
    }
    if (schedule.autoSyncSmartSheet) {
      const syncedResult = await this.automaticallySyncSmartSheetRuns(schedule, result);
      await this.complete(
        schedule,
        trigger,
        startedAt,
        automaticSyncCompletion(syncedResult),
      );
      return;
    }
    if (shouldPersistPendingSync(schedule, result)) {
      try {
        await this.store.appendPendingSync(schedule, result, this.now());
      } catch (error) {
        await this.complete(schedule, trigger, startedAt, {
          message: `任务已完成，但保存腾讯文档待确认结果失败：${errorMessage(error)}`,
          success: false,
          status: "failed",
          result,
        });
        return;
      }
    }
    const completion = workerCompletion(result);
    await this.complete(schedule, trigger, startedAt, {
      ...completion,
      result,
    });
  }

  private async automaticallySyncSmartSheetRuns(
    schedule: ScheduleDefinition,
    result: unknown,
  ): Promise<unknown> {
    let current = result;
    const targets = automaticSyncTargets(result, this.ctx.configPath);
    for (const target of targets) {
      this.emitProgress(
        schedule,
        `正在自动同步“${target.groupName}”到腾讯文档…`,
      );
      if (!target.payload) {
        current = applyAutomaticSyncFailure(
          current,
          target.runIndex,
          target.groupName,
          target.payloadError ?? "缺少腾讯文档预览",
        );
        continue;
      }
      try {
        const response = await this.worker.runWorkerAction("sync", target.payload);
        current = applyAutomaticSyncSuccess(current, target.runIndex, response);
      } catch (error) {
        current = applyAutomaticSyncFailure(
          current,
          target.runIndex,
          target.groupName,
          errorMessage(error),
        );
      }
    }
    return current;
  }

  private emitProgress(schedule: ScheduleDefinition, message: string): void {
    this.ctx.publish("schedule-progress", {
      scheduleId: schedule.id,
      scheduleName: schedule.name,
      message,
    });
  }

  private async complete(
    schedule: ScheduleDefinition,
    trigger: ScheduleExecutionTrigger,
    startedAt: Date,
    completion: ScheduleCompletion,
  ): Promise<void> {
    let message = completion.message;
    let historyPersisted = true;
    try {
      await this.store.appendExecutionHistory({
        schedule,
        trigger,
        startedAt,
        finishedAt: this.now(),
        success: completion.success,
        status: completion.status,
        message,
        result: completion.result,
      });
    } catch (error) {
      this.logger(`保存定时任务执行记录失败：${errorMessage(error)}`);
      message = `${message}；本次执行记录未能保存，请检查配置目录权限或磁盘空间`;
      historyPersisted = false;
    }
    this.ctx.publish("schedule-completed", {
      scheduleId: schedule.id,
      scheduleName: schedule.name,
      message,
      success: completion.success,
      ...(completion.result === undefined ? {} : { result: completion.result }),
      historyPersisted,
    });
  }

  private async runLoop(): Promise<void> {
    while (this.running) {
      const startedAt = this.now().getTime();
      try {
        await this.runTickOnce();
      } catch (error) {
        this.logger(`定时任务检查失败：${errorMessage(error)}`);
      }
      if (!this.running) break;
      const elapsed = this.now().getTime() - startedAt;
      await this.sleep(Math.max(0, this.pollIntervalMs - elapsed));
    }
  }

  private sleep(milliseconds: number): Promise<void> {
    return new Promise<void>((resolve) => {
      const done = () => {
        if (this.timer) {
          clearTimeout(this.timer);
          this.timer = null;
        }
        this.wake = null;
        resolve();
      };
      this.wake = done;
      this.timer = setTimeout(done, milliseconds);
      this.timer.unref?.();
    });
  }
}

function sameScheduleIdentity(saved: unknown[], incoming: unknown[]): boolean {
  const ids = (values: unknown[]): string[] =>
    values
      .map((value) => trimmedString(asRecord(value)?.id) ?? "")
      .sort();
  const savedIds = ids(saved);
  const incomingIds = ids(incoming);
  if (savedIds.length !== incomingIds.length) return false;
  return savedIds.every((id, index) => id === incomingIds[index]);
}

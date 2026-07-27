import type { ProcessingOptions, TaskGroup, TaskResult, TaskRunResult } from "../types";

export const RUN_SESSION_STORAGE_KEY = "wecom-issue-radar:run-session:v1";

export interface RunSessionStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface RunSessionState {
  startDate: string;
  endDate: string;
  startTime: string;
  endTime: string;
  selectedGroups: TaskGroup[];
  options: ProcessingOptions;
  logs: string[];
  result: TaskResult | null;
}

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const RUN_STATUSES = new Set(["success", "empty", "failed"]);
const RESULT_STATUSES = new Set(["success", "partial", "empty", "failed"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isDate(value: unknown): value is string {
  if (typeof value !== "string" || !DATE_PATTERN.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function isTime(value: unknown): value is string {
  return typeof value === "string" && TIME_PATTERN.test(value);
}

function isTaskGroup(value: unknown): value is TaskGroup {
  return isRecord(value)
    && typeof value.id === "string"
    && value.id.trim().length > 0
    && typeof value.name === "string";
}

function isProcessingOptions(value: unknown): value is ProcessingOptions {
  return isRecord(value)
    && typeof value.promptId === "string"
    && typeof value.smartSheetTemplateId === "string"
    && typeof value.runOcr === "boolean"
    && typeof value.runAnalysis === "boolean"
    && typeof value.exportXlsx === "boolean"
    && typeof value.exportMarkdown === "boolean"
    && typeof value.prepareSmartSheet === "boolean";
}

function isOutputs(value: unknown): value is Record<string, string> {
  return isRecord(value) && Object.values(value).every((path) => typeof path === "string");
}

function isOptionalCount(value: unknown): boolean {
  return value === undefined || (typeof value === "number" && Number.isInteger(value) && value >= 0);
}

function isSmartSheetPreview(value: unknown): boolean {
  if (!isRecord(value) || typeof value.pending !== "number" || typeof value.already_synced !== "number") {
    return false;
  }
  return (value.total === undefined || typeof value.total === "number")
    && (value.configured === undefined || typeof value.configured === "boolean")
    && (value.webhook_configured === undefined || typeof value.webhook_configured === "boolean")
    && (value.template_id === undefined || typeof value.template_id === "string")
    && (value.template_name === undefined || typeof value.template_name === "string")
    && (value.template_url === undefined || typeof value.template_url === "string")
    && (value.template_revision === undefined || typeof value.template_revision === "string")
    && (value.document_revision === undefined || typeof value.document_revision === "string")
    && (value.mapping_valid === undefined || typeof value.mapping_valid === "boolean")
    && (value.validation_error === undefined || typeof value.validation_error === "string");
}

function isTaskRunResult(value: unknown): value is TaskRunResult {
  if (!isRecord(value)) return false;
  return typeof value.groupId === "string"
    && typeof value.groupName === "string"
    && (value.status === undefined || (typeof value.status === "string" && RUN_STATUSES.has(value.status)))
    && (value.error === undefined || typeof value.error === "string")
    && typeof value.dayDir === "string"
    && isOutputs(value.outputs)
    && (value.smartSheetTemplateId === undefined || typeof value.smartSheetTemplateId === "string")
    && (value.smartSheetTemplateName === undefined || typeof value.smartSheetTemplateName === "string")
    && (value.smartSheetTemplateUrl === undefined || typeof value.smartSheetTemplateUrl === "string")
    && (value.definitionPath === undefined || value.definitionPath === null || typeof value.definitionPath === "string")
    && isOptionalCount(value.issueCount)
    && (value.smartSheetPreview === undefined || value.smartSheetPreview === null || isSmartSheetPreview(value.smartSheetPreview));
}

function isTaskResult(value: unknown): value is TaskResult {
  if (!isRecord(value) || !Array.isArray(value.runs) || !value.runs.every(isTaskRunResult)) return false;
  return (value.status === undefined || (typeof value.status === "string" && RESULT_STATUSES.has(value.status)))
    && isOptionalCount(value.totalCount)
    && isOptionalCount(value.successCount)
    && isOptionalCount(value.emptyCount)
    && isOptionalCount(value.failedCount)
    && (value.dayDir === undefined || typeof value.dayDir === "string")
    && (value.outputs === undefined || isOutputs(value.outputs))
    && (value.definitionPath === undefined || value.definitionPath === null || typeof value.definitionPath === "string")
    && isOptionalCount(value.issueCount)
    && (value.smartSheetPreview === undefined || value.smartSheetPreview === null || isSmartSheetPreview(value.smartSheetPreview));
}

function isRunSessionState(value: unknown): value is RunSessionState {
  if (!isRecord(value)) return false;
  return isDate(value.startDate)
    && isDate(value.endDate)
    && isTime(value.startTime)
    && isTime(value.endTime)
    && Array.isArray(value.selectedGroups)
    && value.selectedGroups.every(isTaskGroup)
    && isProcessingOptions(value.options)
    && Array.isArray(value.logs)
    && value.logs.every((log) => typeof log === "string")
    && (value.result === null || isTaskResult(value.result))
    && validateRunRange(value.startDate, value.startTime, value.endDate, value.endTime) === null;
}

export function createRunSession(
  today: string,
  selectedGroups: TaskGroup[],
  options: ProcessingOptions,
): RunSessionState {
  return {
    startDate: today,
    endDate: today,
    startTime: "00:00",
    endTime: "23:59",
    selectedGroups,
    options,
    logs: [],
    result: null,
  };
}

export function loadRunSession(storage: RunSessionStorage): RunSessionState | null {
  try {
    const serialized = storage.getItem(RUN_SESSION_STORAGE_KEY);
    if (!serialized) return null;
    const parsed: unknown = JSON.parse(serialized);
    if (!isRecord(parsed) || !isRecord(parsed.options)) return null;
    const migrated = parsed.options.smartSheetTemplateId === undefined
      ? { ...parsed, options: { ...parsed.options, smartSheetTemplateId: "" } }
      : parsed;
    return isRunSessionState(migrated) ? migrated : null;
  } catch {
    return null;
  }
}

export function saveRunSession(storage: RunSessionStorage, state: RunSessionState): void {
  try {
    storage.setItem(RUN_SESSION_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Session restoration is a convenience. A full or unavailable storage must not block exports.
  }
}

export function pendingSmartSheetTemplateIds(result: TaskResult | null): string[] {
  if (!result) return [];
  const ids = new Set<string>();
  result.runs.forEach((run) => {
    if ((run.smartSheetPreview?.pending ?? 0) <= 0) return;
    const templateId = run.smartSheetTemplateId || run.smartSheetPreview?.template_id || "";
    if (templateId) ids.add(templateId);
  });
  return [...ids];
}

export function validateRunRange(
  startDate: string,
  startTime: string,
  endDate: string,
  endTime: string,
): string | null {
  if (!isDate(startDate) || !isDate(endDate) || !isTime(startTime) || !isTime(endTime)) {
    return "请选择完整、有效的导出时间";
  }
  if (startDate > endDate) return "开始日期不能晚于结束日期";
  if (startDate === endDate && startTime > endTime) {
    return "同一天内，开始时间不能晚于结束时间";
  }
  return null;
}

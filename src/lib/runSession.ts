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
    && typeof value.runOcr === "boolean"
    && typeof value.runAnalysis === "boolean"
    && typeof value.exportXlsx === "boolean"
    && typeof value.exportMarkdown === "boolean"
    && typeof value.prepareSmartSheet === "boolean";
}

function isOutputs(value: unknown): value is Record<string, string> {
  return isRecord(value) && Object.values(value).every((path) => typeof path === "string");
}

function isSmartSheetPreview(value: unknown): boolean {
  if (!isRecord(value) || typeof value.pending !== "number" || typeof value.already_synced !== "number") {
    return false;
  }
  return (value.total === undefined || typeof value.total === "number")
    && (value.configured === undefined || typeof value.configured === "boolean");
}

function isTaskRunResult(value: unknown): value is TaskRunResult {
  if (!isRecord(value)) return false;
  return typeof value.groupId === "string"
    && typeof value.groupName === "string"
    && typeof value.dayDir === "string"
    && isOutputs(value.outputs)
    && (value.definitionPath === undefined || value.definitionPath === null || typeof value.definitionPath === "string")
    && (value.smartSheetPreview === undefined || value.smartSheetPreview === null || isSmartSheetPreview(value.smartSheetPreview));
}

function isTaskResult(value: unknown): value is TaskResult {
  if (!isRecord(value) || !Array.isArray(value.runs) || !value.runs.every(isTaskRunResult)) return false;
  return (value.dayDir === undefined || typeof value.dayDir === "string")
    && (value.outputs === undefined || isOutputs(value.outputs))
    && (value.definitionPath === undefined || value.definitionPath === null || typeof value.definitionPath === "string")
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
    return isRunSessionState(parsed) ? parsed : null;
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

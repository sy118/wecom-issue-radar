/** Pure parsing helpers for the Python worker NDJSON protocol. */

export type WorkerEvent =
  | { type: "progress"; message: string }
  | { type: "result"; data: unknown }
  | { type: "error"; message: string; detail?: string }
  | { type: "log"; message: string };

/**
 * Parses a single stdout line from the worker. Non-JSON output is forwarded as
 * a log line (the previous Rust supervisor did the same), while JSON objects
 * without a known `type` are ignored.
 */
export function parseWorkerLine(rawLine: string): WorkerEvent | null {
  const line = rawLine.trim();
  if (!line) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    return { type: "log", message: line };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { type: "log", message: line };
  }
  const record = parsed as Record<string, unknown>;
  switch (record.type) {
    case "progress":
      return typeof record.message === "string"
        ? { type: "progress", message: record.message }
        : null;
    case "result":
      return { type: "result", data: record.data ?? null };
    case "error": {
      const message =
        typeof record.message === "string" && record.message.trim()
          ? record.message
          : "处理引擎执行失败";
      const detail = typeof record.detail === "string" ? record.detail : undefined;
      return detail ? { type: "error", message, detail } : { type: "error", message };
    }
    default:
      return null;
  }
}

export interface WorkerOutcome {
  result: unknown;
  reportedError: string | null;
  logs: string[];
}

export function collectWorkerOutcome(events: Iterable<WorkerEvent>): WorkerOutcome {
  let result: unknown = undefined;
  let reportedError: string | null = null;
  const logs: string[] = [];
  for (const event of events) {
    switch (event.type) {
      case "result":
        result = event.data;
        break;
      case "error":
        reportedError = event.detail ? `${event.message}\n${event.detail}` : event.message;
        break;
      case "log":
        logs.push(event.message);
        break;
      case "progress":
        break;
    }
  }
  return { result, reportedError, logs };
}

export function workerFailureMessage(options: {
  outcome: WorkerOutcome;
  exitCode: number | null;
  stderr: string;
}): string | null {
  const { outcome, exitCode, stderr } = options;
  if (outcome.reportedError) return outcome.reportedError;
  if (exitCode === 0 || exitCode === null) {
    return outcome.result === undefined ? "处理引擎没有返回结果" : null;
  }
  const detail = stderr.trim();
  return detail
    ? `处理引擎执行失败：${detail}`
    : `处理引擎退出，状态码：${exitCode}`;
}

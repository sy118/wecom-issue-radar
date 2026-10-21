import type { AppConfig, GroupInfo, TaskGroup, TaskRequest } from "../types";

export type OnboardingPath = "export" | "reply" | "wechat";

export interface OnboardingProgress {
  version: 1;
  path: OnboardingPath | null;
  step: number;
  completed: boolean;
  skipped: boolean;
}

const STORAGE_KEY = "issue-radar-onboarding.v1";

export const PATH_STEPS: Record<OnboardingPath, string[]> = {
  export: ["detect", "keys", "finish"],
  reply: ["detect", "keys", "model", "listener", "finish"],
  wechat: ["wechat-coming", "finish"],
};

export function emptyProgress(): OnboardingProgress {
  return { version: 1, path: null, step: 0, completed: false, skipped: false };
}

export function loadProgress(): OnboardingProgress {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return emptyProgress();
    const parsed = JSON.parse(raw) as Partial<OnboardingProgress>;
    if (parsed.version !== 1 || typeof parsed.step !== "number") return emptyProgress();
    const path = parsed.path === "export" || parsed.path === "reply" || parsed.path === "wechat"
      ? parsed.path
      : null;
    return {
      version: 1,
      path,
      step: Math.max(0, Math.min(parsed.step, path ? PATH_STEPS[path].length - 1 : 0)),
      completed: Boolean(parsed.completed),
      skipped: Boolean(parsed.skipped),
    };
  } catch {
    return emptyProgress();
  }
}

export function saveProgress(progress: OnboardingProgress): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(progress));
  } catch {
    // Private mode / storage full: the wizard simply restarts next time.
  }
}

export function resetProgress(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}

/** True when the config already points at a real WeCom data directory. */
export function looksConfigured(config: AppConfig): boolean {
  const directory = String(config.wxwork_db_dir || "").trim();
  if (!directory) return false;
  if (directory.includes("<") || directory.includes(">")) return false;
  return directory.length > 6;
}

export function pathLabel(path: OnboardingPath | null): string {
  switch (path) {
    case "export":
      return "只想导出群聊记录";
    case "reply":
      return "想让 AI 在群里自动答";
    case "wechat":
      return "只想给个人微信写草稿";
    default:
      return "还没选择";
  }
}

export function stepLabel(path: OnboardingPath | null, step: number): string {
  if (!path) return "选择一条路径";
  const steps = PATH_STEPS[path];
  const key = steps[Math.max(0, Math.min(step, steps.length - 1))];
  const labels: Record<string, string> = {
    detect: "找到企业微信数据",
    keys: "提取数据库密钥",
    model: "连接大模型（可跳过）",
    listener: "去创建只拟稿监听",
    "wechat-coming": "个人微信（即将上线）",
    finish: "完成",
  };
  return labels[key] ?? "继续";
}

/** Local calendar date (`YYYY-MM-DD`) used by the one-day trial export. */
export function localDateString(date: Date = new Date()): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** Normalize the two shapes the sidecar returns for a conversation into a task group. */
export function toTaskGroup(group: GroupInfo | null | undefined): TaskGroup | null {
  if (!group) return null;
  const rawId = group.conversation_id ?? group.id;
  const id = typeof rawId === "string" ? rawId.trim() : "";
  if (!id) return null;
  const rawName = group.display_name ?? group.name;
  const name = typeof rawName === "string" && rawName.trim() ? rawName.trim() : id;
  return { id, name };
}

/** First usable conversation; used by the wizard so trial export needs no extra picker. */
export function pickFirstGroup(groups: GroupInfo[] | null | undefined): TaskGroup | null {
  if (!Array.isArray(groups)) return null;
  for (const group of groups) {
    const normalized = toTaskGroup(group);
    if (normalized) return normalized;
  }
  return null;
}

/**
 * Minimal, explicitly read-only export request for the onboarding "try today" button.
 * Never runs OCR/analysis, never prepares or writes a Tencent Doc, and only covers today.
 */
export function buildTrialExportRequest(options: {
  date: string;
  group: TaskGroup;
  promptId: string;
}): TaskRequest {
  const date = options.date;
  return {
    startDate: date,
    endDate: date,
    date,
    startTime: "00:00",
    endTime: "23:59",
    groups: [{ id: options.group.id, name: options.group.name }],
    promptId: options.promptId,
    smartSheetTemplateId: "",
    runOcr: false,
    runAnalysis: false,
    exportXlsx: true,
    exportMarkdown: true,
    prepareSmartSheet: false,
  };
}

/** Steps already passed, so the workbench can show `n/m` instead of a bare label. */
export function stepProgress(progress: OnboardingProgress): { done: number; total: number } {
  if (!progress.path) return { done: 0, total: 0 };
  const total = PATH_STEPS[progress.path].length;
  return { done: progress.completed ? total : Math.min(Math.max(0, progress.step), total), total };
}

/** Compact human label for "继续设置" and settings summaries. */
export function progressSummary(progress: OnboardingProgress): string {
  if (!progress.path) return progress.completed ? "已跳过引导" : "还没选择路径";
  const { done, total } = stepProgress(progress);
  return done >= total ? `已完成 ${total} 步` : `已完成 ${done}/${total} 步`;
}

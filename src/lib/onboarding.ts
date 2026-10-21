import type { AppConfig } from "../types";

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
  reply: ["detect", "keys", "model", "finish"],
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
    "wechat-coming": "个人微信（即将上线）",
    finish: "完成",
  };
  return labels[key] ?? "继续";
}

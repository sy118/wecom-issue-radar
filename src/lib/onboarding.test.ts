import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  PATH_STEPS,
  buildTrialExportRequest,
  emptyProgress,
  loadProgress,
  localDateString,
  looksConfigured,
  pathLabel,
  pickFirstGroup,
  progressSummary,
  resetProgress,
  saveProgress,
  stepLabel,
  stepProgress,
  toTaskGroup,
} from "./onboarding";
import type { AppConfig } from "../types";

function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (key: string) => data.get(key) ?? null,
    key: (index: number) => [...data.keys()][index] ?? null,
    removeItem: (key: string) => {
      data.delete(key);
    },
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  } as Storage;
}

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryStorage());
  resetProgress();
});

describe("onboarding state", () => {
  it("starts empty and persists a path with its step", () => {
    expect(loadProgress()).toEqual(emptyProgress());
    saveProgress({ version: 1, path: "reply", step: 2, completed: false, skipped: false });
    expect(loadProgress()).toMatchObject({ path: "reply", step: 2, completed: false });
  });

  it("clamps steps to the selected path length", () => {
    saveProgress({ version: 1, path: "export", step: 99, completed: false, skipped: false });
    expect(loadProgress().step).toBe(PATH_STEPS.export.length - 1);
  });

  it("falls back to an empty progress on invalid storage", () => {
    localStorage.setItem("issue-radar-onboarding.v1", "{broken");
    expect(loadProgress()).toEqual(emptyProgress());
    localStorage.setItem("issue-radar-onboarding.v1", JSON.stringify({ version: 99 }));
    expect(loadProgress()).toEqual(emptyProgress());
  });

  it("labels paths and steps for the wizard", () => {
    expect(pathLabel("export")).toContain("导出");
    expect(pathLabel("wechat")).toContain("个人微信");
    expect(stepLabel("reply", 0)).toContain("数据");
    expect(stepLabel("reply", 2)).toContain("模型");
    expect(stepLabel(null, 0)).toContain("路径");
  });
});

describe("configured detection", () => {
  const base = { wxwork_db_dir: "" } as AppConfig;

  it("treats an empty or placeholder directory as not configured", () => {
    expect(looksConfigured(base)).toBe(false);
    expect(looksConfigured({ ...base, wxwork_db_dir: "C:\\Users\\<user>\\Documents\\WXWork" })).toBe(false);
  });

  it("treats a real directory as configured so existing users skip the wizard", () => {
    expect(
      looksConfigured({ ...base, wxwork_db_dir: "C:\\Users\\admin\\Documents\\WXWork\\abc\\Data" }),
    ).toBe(true);
  });
});

describe("trial export request", () => {
  const group = { id: "R:123", name: "客户群 A" };

  it("only covers one day, one group, and file exports", () => {
    const request = buildTrialExportRequest({ date: "2026-10-21", group, promptId: "prompt-1" });

    expect(request.startDate).toBe("2026-10-21");
    expect(request.endDate).toBe("2026-10-21");
    expect(request.startTime).toBe("00:00");
    expect(request.endTime).toBe("23:59");
    expect(request.groups).toEqual([group]);
    expect(request.promptId).toBe("prompt-1");
    expect(request.exportXlsx).toBe(true);
    expect(request.exportMarkdown).toBe(true);
  });

  it("never asks for OCR, model analysis, or a Tencent Doc write", () => {
    const request = buildTrialExportRequest({ date: "2026-10-21", group, promptId: "p" });

    expect(request.runOcr).toBe(false);
    expect(request.runAnalysis).toBe(false);
    expect(request.prepareSmartSheet).toBe(false);
    expect(request.smartSheetTemplateId).toBe("");
  });

  it("formats the local date without shifting time zones", () => {
    expect(localDateString(new Date(2026, 9, 21, 23, 30))).toBe("2026-10-21");
    expect(localDateString(new Date(2026, 0, 2, 0, 5))).toBe("2026-01-02");
  });
});

describe("conversation normalization", () => {
  it("accepts both conversation_id and id shapes", () => {
    expect(toTaskGroup({ conversation_id: "R:1", name: "群一" })).toEqual({ id: "R:1", name: "群一" });
    expect(toTaskGroup({ id: "R:2", display_name: "群二" })).toEqual({ id: "R:2", name: "群二" });
  });

  it("falls back to the id as the name and rejects unusable rows", () => {
    expect(toTaskGroup({ id: "R:3" })).toEqual({ id: "R:3", name: "R:3" });
    expect(toTaskGroup({ name: "只有名字" })).toBeNull();
    expect(toTaskGroup(null)).toBeNull();
    expect(pickFirstGroup([{ name: "只有名字" }, { id: "R:9", name: "可用" }])).toEqual({ id: "R:9", name: "可用" });
    expect(pickFirstGroup([])).toBeNull();
  });
});

describe("guide progress summary", () => {
  it("counts passed steps for the workbench hint", () => {
    const midway = { version: 1 as const, path: "reply" as const, step: 2, completed: false, skipped: false };
    expect(stepProgress(midway)).toEqual({ done: 2, total: PATH_STEPS.reply.length });
    expect(progressSummary(midway)).toBe(`已完成 2/${PATH_STEPS.reply.length} 步`);

    const done = { ...midway, step: PATH_STEPS.reply.length - 1, completed: true };
    expect(stepProgress(done)).toEqual({ done: PATH_STEPS.reply.length, total: PATH_STEPS.reply.length });
    expect(progressSummary(done)).toBe(`已完成 ${PATH_STEPS.reply.length} 步`);
  });

  it("describes an unselected or skipped guide", () => {
    expect(progressSummary(emptyProgress())).toBe("还没选择路径");
    expect(stepProgress(emptyProgress())).toEqual({ done: 0, total: 0 });
    expect(progressSummary({ ...emptyProgress(), completed: true, skipped: true })).toBe("已跳过引导");
  });

  it("puts the draft-only listener step before finish on the reply path", () => {
    const steps = PATH_STEPS.reply;
    expect(steps.indexOf("listener")).toBeGreaterThan(steps.indexOf("model"));
    expect(steps.indexOf("listener")).toBe(steps.length - 2);
    expect(stepLabel("reply", steps.indexOf("listener"))).toContain("监听");
  });
});

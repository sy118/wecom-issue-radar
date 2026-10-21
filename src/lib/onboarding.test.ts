import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  PATH_STEPS,
  emptyProgress,
  loadProgress,
  looksConfigured,
  pathLabel,
  resetProgress,
  saveProgress,
  stepLabel,
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

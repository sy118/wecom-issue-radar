import { describe, expect, it } from "vitest";
import type { ProcessingOptions, TaskResult } from "../types";
import {
  createRunSession,
  loadRunSession,
  RUN_SESSION_STORAGE_KEY,
  saveRunSession,
  validateRunRange,
  type RunSessionState,
} from "./runSession";

class MemoryStorage {
  private readonly values = new Map<string, string>();

  getItem(key: string) {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string) {
    this.values.set(key, value);
  }
}

const options: ProcessingOptions = {
  promptId: "daily",
  runOcr: true,
  runAnalysis: true,
  exportXlsx: true,
  exportMarkdown: false,
  prepareSmartSheet: false,
};

describe("run session persistence", () => {
  it("defaults both ends of the range to today", () => {
    const session = createRunSession("2026-07-24", [], options);

    expect(session.startDate).toBe("2026-07-24");
    expect(session.endDate).toBe("2026-07-24");
    expect(session.startTime).toBe("00:00");
    expect(session.endTime).toBe("23:59");
  });

  it("restores selections, logs, and the most recent export result", () => {
    const storage = new MemoryStorage();
    const result: TaskResult = {
      runs: [{
        groupId: "group-1",
        groupName: "产品群",
        dayDir: "D:/exports/2026-07-24/product",
        outputs: { xlsx: "D:/exports/2026-07-24/product/issues.xlsx" },
      }],
    };
    const snapshot: RunSessionState = {
      startDate: "2026-07-23",
      endDate: "2026-07-24",
      startTime: "18:30",
      endTime: "09:15",
      selectedGroups: [{ id: "group-1", name: "产品群" }],
      options,
      logs: ["任务完成"],
      result,
    };

    saveRunSession(storage, snapshot);

    expect(loadRunSession(storage)).toEqual(snapshot);
  });

  it("ignores malformed or structurally invalid stored data", () => {
    const malformed = new MemoryStorage();
    malformed.setItem(RUN_SESSION_STORAGE_KEY, "{not-json");
    expect(loadRunSession(malformed)).toBeNull();

    const invalid = new MemoryStorage();
    invalid.setItem(RUN_SESSION_STORAGE_KEY, JSON.stringify({ startDate: "2026-07-24" }));
    expect(loadRunSession(invalid)).toBeNull();
  });
});

describe("run range validation", () => {
  it("accepts an overnight range when the end date is later", () => {
    expect(validateRunRange("2026-07-23", "23:00", "2026-07-24", "01:00")).toBeNull();
  });

  it("rejects a reversed date range", () => {
    expect(validateRunRange("2026-07-25", "00:00", "2026-07-24", "23:59")).toBe("开始日期不能晚于结束日期");
  });

  it("rejects a reversed time range on the same day", () => {
    expect(validateRunRange("2026-07-24", "18:00", "2026-07-24", "09:00")).toBe("同一天内，开始时间不能晚于结束时间");
  });

  it("rejects invalid calendar dates and clock values", () => {
    expect(validateRunRange("2026-02-30", "00:00", "2026-03-01", "23:59")).toBe("请选择完整、有效的导出时间");
    expect(validateRunRange("2026-03-01", "24:00", "2026-03-01", "23:59")).toBe("请选择完整、有效的导出时间");
  });
});

import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import {
  ScheduleStateStore,
  applyAutomaticSyncFailure,
  applyAutomaticSyncSuccess,
  automaticSyncCompletion,
  automaticSyncTargets,
  buildAutomaticSyncPayload,
  buildWorkerRunRequest,
  claimScheduleRun,
  createEmptyScheduleState,
  isScheduleDue,
  missedMinuteSlots,
  missingPendingTemplateReference,
  paginateExecutionHistory,
  parseSchedule,
  pushExecutionHistoryItem,
  resolveExportRange,
  resultHasPendingSmartSheetRecords,
  shanghaiClock,
  shouldPersistPendingSync,
  validateHistoryPagination,
  validateSchedule,
  workerCompletion,
  type ScheduleDefinition,
  type ScheduleExecutionHistoryItem,
  type ScheduleState,
} from "../scheduler";

function sampleSchedule(overrides: Partial<ScheduleDefinition> = {}): ScheduleDefinition {
  return {
    id: "weekday_export",
    name: "工作日导出",
    enabled: true,
    runAt: "18:30",
    weekdays: [1, 2, 3, 4, 5],
    dateMode: "today",
    fixedDate: "2026-07-20",
    startTime: "00:00",
    endTime: "23:59",
    groups: [{ id: "R:group", name: "产品群" }],
    promptId: "daily",
    smartSheetTemplateId: "default",
    runOcr: true,
    runAnalysis: true,
    exportXlsx: true,
    exportMarkdown: true,
    prepareSmartSheet: true,
    autoSyncSmartSheet: false,
    ...overrides,
  };
}

function historyItem(
  overrides: Partial<ScheduleExecutionHistoryItem> = {},
): ScheduleExecutionHistoryItem {
  return {
    executionId: "exec-1",
    scheduleId: "weekday_export",
    scheduleName: "工作日导出",
    trigger: "automatic",
    startedAt: "2026-07-20T10:00:00.000Z",
    finishedAt: "2026-07-20T10:00:05.000Z",
    success: true,
    status: "success",
    message: "任务执行完成",
    ...overrides,
  };
}

describe("schedule due decisions", () => {
  it("is due only when enabled, at the exact minute, on a configured weekday", () => {
    const schedule = sampleSchedule();
    expect(isScheduleDue(schedule, 1, "18:30")).toBe(true);
    expect(isScheduleDue(schedule, 7, "18:30")).toBe(false);
    expect(isScheduleDue(schedule, 1, "18:31")).toBe(false);
    expect(isScheduleDue(schedule, 1, "17:30")).toBe(false);
    expect(isScheduleDue({ ...schedule, enabled: false }, 1, "18:30")).toBe(false);
  });

  it("uses the Shanghai clock for date, minute and ISO weekday", () => {
    const clock = shanghaiClock(new Date("2026-07-20T10:05:00Z"));
    expect(clock).toEqual({ date: "2026-07-20", minute: "18:05", weekday: 1 });
    // 2026-07-19T16:30:00Z is Monday 00:30 in Shanghai.
    expect(shanghaiClock(new Date("2026-07-19T16:30:00Z"))).toEqual({
      date: "2026-07-20",
      minute: "00:30",
      weekday: 1,
    });
  });
});

describe("claim semantics", () => {
  it("allows one automatic run per schedule and date", () => {
    const state = createEmptyScheduleState();
    expect(claimScheduleRun(state, "schedule-a", "2026-07-20", "18:30")).toBe(true);
    // Same date but a different (catch-up) minute must not run twice.
    expect(claimScheduleRun(state, "schedule-a", "2026-07-20", "18:31")).toBe(false);
    expect(state.lastRuns["schedule-a"]).toBe("2026-07-20 18:30");
    expect(claimScheduleRun(state, "schedule-a", "2026-07-21", "18:30")).toBe(true);
    // Schedules are claimed independently.
    expect(claimScheduleRun(state, "schedule-b", "2026-07-20", "18:30")).toBe(true);
  });

  it("persists the claim atomically through the state store", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "scheduler-state-"));
    try {
      const store = new ScheduleStateStore(path.join(directory, "schedule-state.json"));
      expect(await store.claimRun("schedule-a", "2026-07-20", "18:30")).toBe(true);
      expect(await store.claimRun("schedule-a", "2026-07-20", "18:30")).toBe(false);
      const persisted = await store.read();
      expect(persisted.lastRuns["schedule-a"]).toBe("2026-07-20 18:30");
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
});

describe("catch-up after sleep", () => {
  it("produces no slots when the previous tick is recent", () => {
    const now = new Date("2026-07-20T10:00:00Z");
    expect(missedMinuteSlots(new Date(now.getTime() - 30_000), now)).toEqual([]);
    expect(missedMinuteSlots(new Date(now.getTime() - 90_000), now)).toEqual([]);
  });

  it("covers every missed minute strictly before the current minute", () => {
    const now = new Date("2026-07-20T10:00:00Z");
    const lastTick = new Date("2026-07-20T09:55:30Z");
    const slots = missedMinuteSlots(lastTick, now);
    expect(slots.map((slot) => slot.minute)).toEqual([
      "17:56",
      "17:57",
      "17:58",
      "17:59",
    ]);
    expect(slots.every((slot) => slot.date === "2026-07-20")).toBe(true);
  });
});

describe("export range resolution", () => {
  it("resolves today/yesterday/fixed dates", () => {
    expect(resolveExportRange(sampleSchedule(), "2026-07-20")).toEqual({
      startDate: "2026-07-20",
      endDate: "2026-07-20",
    });
    expect(
      resolveExportRange(sampleSchedule({ dateMode: "yesterday" }), "2026-07-20"),
    ).toEqual({ startDate: "2026-07-19", endDate: "2026-07-19" });
    expect(
      resolveExportRange(
        sampleSchedule({ dateMode: "fixed", fixedDate: "2026-07-01" }),
        "2026-07-20",
      ),
    ).toEqual({ startDate: "2026-07-01", endDate: "2026-07-01" });
  });

  it("rolls the end date to the next day for overnight ranges", () => {
    expect(
      resolveExportRange(
        sampleSchedule({ startTime: "22:00", endTime: "02:00" }),
        "2026-07-20",
      ),
    ).toEqual({ startDate: "2026-07-20", endDate: "2026-07-21" });
  });

  it("rejects invalid fixed dates", () => {
    expect(() =>
      resolveExportRange(
        sampleSchedule({ dateMode: "fixed", fixedDate: "2026-02-31" }),
        "2026-07-20",
      ),
    ).toThrow("固定日期无效");
  });
});

describe("worker run request assembly", () => {
  it("matches the legacy run_task payload shape", () => {
    const request = buildWorkerRunRequest(sampleSchedule(), "2026-07-20", "2026-07-20");
    expect(request).toMatchObject({
      date: "2026-07-20",
      startDate: "2026-07-20",
      endDate: "2026-07-20",
      startTime: "00:00",
      endTime: "23:59",
      promptId: "daily",
      smartSheetTemplateId: "default",
      runOcr: true,
      runAnalysis: true,
      exportXlsx: true,
      exportMarkdown: true,
      prepareSmartSheet: true,
    });
    expect(request.groups).toEqual([{ id: "R:group", name: "产品群" }]);
  });
});

describe("execution history pagination", () => {
  it("sorts newest first, filters by schedule and computes pages", () => {
    const state: ScheduleState = {
      lastRuns: {},
      pendingSmartSheetSyncs: [],
      executionHistory: [
        historyItem({ executionId: "old", finishedAt: "2026-07-18T10:00:00.000Z" }),
        historyItem({ executionId: "new", finishedAt: "2026-07-20T10:00:00.000Z" }),
        historyItem({
          executionId: "other",
          scheduleId: "other-schedule",
          finishedAt: "2026-07-19T10:00:00.000Z",
        }),
      ],
    };
    const page = paginateExecutionHistory(state, 1, 2);
    expect(page.total).toBe(3);
    expect(page.totalPages).toBe(2);
    expect(page.items.map((item) => item.executionId)).toEqual(["new", "other"]);

    const filtered = paginateExecutionHistory(state, 1, 50, "weekday_export");
    expect(filtered.total).toBe(2);
    expect(filtered.items.map((item) => item.executionId)).toEqual(["new", "old"]);

    const second = paginateExecutionHistory(state, 2, 2);
    expect(second.items.map((item) => item.executionId)).toEqual(["old"]);
  });

  it("validates pagination bounds", () => {
    expect(() => validateHistoryPagination(0, 10)).toThrow("页码必须从 1 开始");
    expect(() => validateHistoryPagination(1, 0)).toThrow("每页数量");
    expect(() => validateHistoryPagination(1, 51)).toThrow("每页数量");
    expect(() => validateHistoryPagination(1, 50)).not.toThrow();
  });

  it("caps history at 500 entries and keeps ids unique", () => {
    const state = createEmptyScheduleState();
    const startedAt = new Date("2026-07-20T10:00:00.000Z");
    for (let index = 0; index < 505; index += 1) {
      pushExecutionHistoryItem(state, {
        schedule: sampleSchedule(),
        trigger: "manual",
        startedAt,
        finishedAt: new Date(startedAt.getTime() + index),
        success: true,
        status: "success",
        message: `run ${index}`,
      });
    }
    expect(state.executionHistory).toHaveLength(500);
    expect(new Set(state.executionHistory.map((item) => item.executionId)).size).toBe(500);
    expect(state.executionHistory[0]?.message).toBe("run 5");
    expect(state.executionHistory[499]?.message).toBe("run 504");
  });
});

describe("schedule parsing and validation", () => {
  it("parses camelCase schedules and defaults legacy fields", () => {
    const parsed = parseSchedule({
      id: "s1",
      name: "每日导出",
      enabled: true,
      runAt: "09:00",
      weekdays: [1, 2],
      dateMode: "yesterday",
      startTime: "00:00",
      endTime: "23:59",
      groups: [{ id: "R:a", name: "A 群" }],
    });
    expect(parsed.autoSyncSmartSheet).toBe(false);
    expect(parsed.smartSheetTemplateId).toBe("");
    expect(parsed.promptId).toBe("");
  });

  it("rejects auto sync without analysis or a frozen template", () => {
    const base = sampleSchedule({
      autoSyncSmartSheet: true,
      runAnalysis: true,
      prepareSmartSheet: true,
      smartSheetTemplateId: "default",
    });
    expect(() => validateSchedule(base)).not.toThrow();
    expect(() =>
      validateSchedule({ ...base, runAnalysis: false }),
    ).toThrow("必须启用大模型分析");
    expect(() =>
      validateSchedule({ ...base, smartSheetTemplateId: " " }),
    ).toThrow("必须冻结腾讯文档模板");
  });
});

describe("execution result handling", () => {
  it("maps aggregate statuses to completion messages", () => {
    expect(workerCompletion({ status: "success" }).message).toBe("任务执行完成");
    expect(workerCompletion({ status: "partial" }).success).toBe(true);
    expect(workerCompletion({ status: "empty" }).message).toContain("无可分析记录");
    expect(workerCompletion({ status: "failed" })).toMatchObject({
      success: false,
      status: "failed",
    });
  });

  it("only queues manual Smart Sheet confirmations when pending", () => {
    const schedule = sampleSchedule();
    const pendingResult = {
      runs: [{ status: "success", smartSheetPreview: { pending: 2 } }],
    };
    expect(resultHasPendingSmartSheetRecords(pendingResult)).toBe(true);
    expect(shouldPersistPendingSync(schedule, pendingResult)).toBe(true);
    expect(
      shouldPersistPendingSync({ ...schedule, autoSyncSmartSheet: true }, pendingResult),
    ).toBe(false);
    expect(
      resultHasPendingSmartSheetRecords({
        runs: [{ status: "success", smartSheetPreview: { pending: 0 } }],
      }),
    ).toBe(false);
  });
});

describe("automatic Smart Sheet sync", () => {
  const run = {
    groupId: "R:group",
    groupName: "产品群",
    status: "success",
    dayDir: "C:/exports/2026-07-20",
    endDate: "2026-07-20",
    smartSheetTemplateId: "default",
    definitionPath: "C:/exports/2026-07-20/issues.json",
    smartSheetPreview: {
      pending: 3,
      already_synced: 1,
      template_id: "default",
      template_revision: "template-r1",
      document_revision: "document-r2",
      image_integrity: { mapped: true, expected: 5, missing: 2 },
    },
  };

  it("builds the guarded automatic sync payload", () => {
    expect(buildAutomaticSyncPayload(run, "C:/config.json")).toMatchObject({
      configPath: "C:/config.json",
      dayDir: "C:/exports/2026-07-20",
      date: "2026-07-20",
      templateId: "default",
      uploadImages: true,
      allowMissingImages: true,
      definitionPath: "C:/exports/2026-07-20/issues.json",
      expectedTemplateRevision: "template-r1",
      expectedDocumentRevision: "document-r2",
    });
  });

  it("only targets runs with a pending preview", () => {
    const targets = automaticSyncTargets(
      {
        runs: [
          run,
          { ...run, groupId: "R:empty", smartSheetPreview: { pending: 0 } },
        ],
      },
      "C:/config.json",
    );
    expect(targets).toHaveLength(1);
    expect(targets[0]).toMatchObject({ runIndex: 0, groupName: "产品群" });
  });

  it("applies success and failure to the run result", () => {
    const result = { runs: [{ ...run }], status: "success" };
    applyAutomaticSyncSuccess(result, 0, {
      synced: 3,
      total: 4,
      image_count: 3,
    });
    const syncedRun = result.runs[0] as unknown as Record<string, unknown>;
    expect(syncedRun.smartSheetSync).toMatchObject({
      mode: "automatic",
      status: "success",
      synced: 3,
      missingImages: 2,
    });
    expect(
      (syncedRun.smartSheetPreview as Record<string, unknown>).pending,
    ).toBe(0);
    expect(result.status).toBe("success");

    applyAutomaticSyncFailure(result, 0, "产品群", "网络超时");
    expect(syncedRun.status).toBe("failed");
    expect(result.status).toBe("failed");
    const completion = automaticSyncCompletion(result);
    expect(completion.success).toBe(false);
    expect(completion.message).toContain("自动同步失败");
  });
});

describe("pending template reference guard", () => {
  it("detects a template that a pending confirmation still needs", () => {
    const pending = {
      pendingId: "p1",
      scheduleId: "weekday_export",
      scheduleName: "工作日导出",
      createdAt: "2026-07-20T10:00:00.000Z",
      result: {
        runs: [
          {
            smartSheetPreview: { pending: 1, template_id: "default" },
            smartSheetTemplateName: "默认模板",
          },
        ],
      },
    };
    const config = {
      smart_sheet: { templates: [{ id: "default" }] },
    };
    expect(missingPendingTemplateReference(config, [pending])).toBeNull();
    expect(
      missingPendingTemplateReference(
        { smart_sheet: { templates: [{ id: "other" }] } },
        [pending],
      ),
    ).toMatchObject({ scheduleName: "工作日导出", templateId: "default" });
  });
});

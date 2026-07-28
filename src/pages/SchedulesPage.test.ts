import { describe, expect, it } from "vitest";
import type {
  AppConfig,
  PendingScheduleSync,
  ScheduleDefinition,
  ScheduleExecutionHistoryItem,
} from "../types";
import {
  automaticSyncWarningSummary,
  canAutoSyncSmartSheet,
  createPendingSyncBatch,
  executionHistoryCounts,
  executionHistoryRunDetail,
  executionHistoryStatusLabel,
  isCurrentConfirmationRequest,
  isCurrentHistoryRequest,
  newSchedule,
  normalizeScheduleAutoSync,
  orphanPendingSyncGroupsFrom,
  scheduleForEditing,
  withoutPendingSyncBatch,
} from "./SchedulesPage";

function appConfig(): AppConfig {
  return {
    wxwork_db_dir: "",
    wxwork_keys_file: "",
    target_group_id: "",
    target_group_name: "",
    default_workspace: "D:/exports",
    timezone: "Asia/Shanghai",
    ocr: { base_url: "", api_key: "", model: "" },
    llm: { base_url: "", api_key: "key", model: "model" },
    prompts: {
      default_id: "prompt-a",
      items: [{
        id: "prompt-a",
        name: "默认提示词",
        description: "",
        content: "",
        issue_fields: [],
      }],
    },
    smart_sheet: {
      default_template_id: "template-a",
      templates: [{
        id: "template-a",
        name: "问题清单",
        url: "",
        webhook_url_env: "",
        webhook_url: "",
        batch_size: 10,
        schema: {},
        field_mappings: [],
      }],
      upload: {
        token_endpoint: "",
        image_upload_endpoint: "",
        image_form_field: "media",
        propagation_wait_ms_after_uploads: 0,
        delay_ms_between_image_uploads: 0,
        corpid: "",
        corpsecret: "",
      },
    },
  };
}

function autoSyncSchedule(patch: Partial<ScheduleDefinition> = {}): ScheduleDefinition {
  return {
    id: "daily",
    name: "日报",
    enabled: true,
    autoSyncSmartSheet: true,
    runAt: "18:30",
    weekdays: [1, 2, 3, 4, 5],
    dateMode: "today",
    fixedDate: "2026-07-27",
    startTime: "00:00",
    endTime: "23:59",
    groups: [{ id: "room-a", name: "产品群" }],
    promptId: "prompt-a",
    smartSheetTemplateId: "template-a",
    runOcr: false,
    runAnalysis: true,
    exportXlsx: true,
    exportMarkdown: true,
    prepareSmartSheet: true,
    ...patch,
  };
}

function pendingSync(
  pendingId: string,
  scheduleId: string,
  scheduleName: string,
  createdAt: string,
  pending: number,
): PendingScheduleSync {
  return {
    pendingId,
    scheduleId,
    scheduleName,
    createdAt,
    result: {
      runs: [{
        groupId: `group-${pendingId}`,
        groupName: "产品群",
        dayDir: `D:/exports/${pendingId}`,
        outputs: {},
        smartSheetPreview: { pending, already_synced: 0 },
      }],
    },
  };
}

describe("schedule automatic Smart Sheet sync", () => {
  it("defaults new and legacy schedules to manual confirmation", () => {
    const config = appConfig();

    expect(newSchedule(config).autoSyncSmartSheet).toBe(false);
    expect(normalizeScheduleAutoSync(config, autoSyncSchedule({
      autoSyncSmartSheet: undefined,
    })).autoSyncSmartSheet).toBe(false);
  });

  it("allows automatic sync only with analysis, preview, and a valid template", () => {
    const config = appConfig();
    expect(canAutoSyncSmartSheet(config, autoSyncSchedule())).toBe(true);
    expect(normalizeScheduleAutoSync(config, autoSyncSchedule()).autoSyncSmartSheet).toBe(true);

    for (const patch of [
      { runAnalysis: false },
      { prepareSmartSheet: false },
      { smartSheetTemplateId: "missing-template" },
    ]) {
      const schedule = autoSyncSchedule(patch);
      expect(canAutoSyncSmartSheet(config, schedule)).toBe(false);
      expect(normalizeScheduleAutoSync(config, schedule).autoSyncSmartSheet).toBe(false);
    }
  });

  it("does not silently keep automatic sync when an old template falls back", () => {
    const editor = scheduleForEditing(appConfig(), autoSyncSchedule({
      smartSheetTemplateId: "deleted-template",
    }));

    expect(editor.smartSheetTemplateId).toBe("template-a");
    expect(editor.autoSyncSmartSheet).toBe(false);
  });
});

describe("schedule pending helpers", () => {
  it("groups only pending results whose schedule no longer exists", () => {
    const groups = orphanPendingSyncGroupsFrom(
      [{ id: "active" }],
      [
        pendingSync("active:1", "active", "仍在运行", "2026-07-24T10:00:00Z", 9),
        pendingSync("deleted:1", "deleted", "旧名称", "2026-07-24T11:00:00Z", 2),
        pendingSync("deleted:2", "deleted", "已删除任务", "2026-07-24T12:00:00Z", 3),
      ],
    );

    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      scheduleId: "deleted",
      scheduleName: "已删除任务",
      latestCreatedAt: "2026-07-24T12:00:00Z",
      pendingCount: 5,
    });
    expect(groups[0].items.map((item) => item.pendingId)).toEqual(["deleted:1", "deleted:2"]);
  });

  it("keeps a legacy top-level pending run visible instead of treating it as empty", () => {
    const legacy: PendingScheduleSync = {
      pendingId: "legacy:1",
      scheduleId: "legacy",
      scheduleName: "旧单群任务",
      createdAt: "2026-07-24T12:00:00Z",
      result: {
        dayDir: "D:/exports/legacy",
        outputs: {},
        smartSheetPreview: { pending: 4, already_synced: 0 },
      } as PendingScheduleSync["result"],
    };

    const groups = orphanPendingSyncGroupsFrom([], [legacy]);

    expect(groups).toHaveLength(1);
    expect(groups[0].pendingCount).toBe(4);
  });

  it("clears only the batch frozen when confirmation opened", () => {
    const first = pendingSync("daily:1", "daily", "日报", "2026-07-24T12:00:00Z", 2);
    const frozenBatch = createPendingSyncBatch("daily", [first]);
    const arrivedWhileOpen = pendingSync("daily:2", "daily", "日报", "2026-07-24T12:05:00Z", 3);
    const livePending = [first, arrivedWhileOpen];

    expect(frozenBatch.pendingIds).toEqual(["daily:1"]);
    expect(withoutPendingSyncBatch(livePending, frozenBatch).map((item) => item.pendingId))
      .toEqual(["daily:2"]);
  });

  it("rejects a stale preview response after close and reopen", () => {
    let currentGeneration = 0;
    const requestA = ++currentGeneration;
    currentGeneration += 1; // Closing A invalidates its in-flight preview.
    const requestB = ++currentGeneration;
    let committedBatch = "";
    const commit = (requestGeneration: number, batch: string) => {
      if (isCurrentConfirmationRequest(requestGeneration, currentGeneration)) {
        committedBatch = batch;
      }
    };

    expect(isCurrentConfirmationRequest(requestA, currentGeneration)).toBe(false);
    expect(isCurrentConfirmationRequest(requestB, currentGeneration)).toBe(true);
    commit(requestA, "A");
    expect(committedBatch).toBe("");
    commit(requestB, "B");
    expect(committedBatch).toBe("B");
  });
});

describe("schedule execution history helpers", () => {
  const item = (
    patch: Partial<ScheduleExecutionHistoryItem>,
  ): ScheduleExecutionHistoryItem => ({
    executionId: "run-1",
    scheduleId: "daily",
    scheduleName: "日报",
    trigger: "automatic",
    startedAt: "2026-07-27T10:00:00Z",
    finishedAt: "2026-07-27T10:01:00Z",
    success: true,
    status: "success",
    message: "任务执行完成",
    ...patch,
  });

  it("distinguishes partial, empty, failed, and legacy successful executions", () => {
    expect(executionHistoryStatusLabel(item({ status: "partial" }))).toBe("部分完成");
    expect(executionHistoryStatusLabel(item({ status: "empty" }))).toBe("无可分析记录");
    expect(executionHistoryStatusLabel(item({ status: "failed", success: false }))).toBe("执行失败");
    expect(executionHistoryStatusLabel({ success: true })).toBe("执行成功");
  });

  it("uses aggregate counts when present and falls back to per-group statuses", () => {
    expect(executionHistoryCounts({
      runs: [],
      successCount: 4,
      emptyCount: 2,
      failedCount: 1,
    })).toEqual({ success: 4, empty: 2, failed: 1 });

    expect(executionHistoryCounts({
      runs: [
        { groupId: "a", groupName: "A", status: "success", dayDir: "D:/a", outputs: {} },
        { groupId: "b", groupName: "B", status: "empty", dayDir: "D:/b", outputs: {} },
        { groupId: "c", groupName: "C", status: "failed", dayDir: "", outputs: {} },
      ],
    })).toEqual({ success: 1, empty: 1, failed: 1 });
  });

  it("shows automatic sync counts while keeping the existing group error first", () => {
    expect(executionHistoryRunDetail({
      groupId: "a",
      groupName: "销售群",
      status: "success",
      dayDir: "D:/a",
      outputs: { xlsx: "D:/a/issues.xlsx" },
      smartSheetSync: {
        mode: "automatic",
        status: "success",
        synced: 3,
      },
    })).toContain("已自动同步 3 条");

    expect(executionHistoryRunDetail({
      groupId: "b",
      groupName: "客服群",
      status: "failed",
      error: "现有群错误",
      dayDir: "D:/b",
      outputs: {},
      smartSheetSync: {
        mode: "automatic",
        status: "failed",
        error: "结构化同步错误",
      },
    })).toBe("现有群错误");
  });

  it("keeps a missing-image sync successful while surfacing its warning", () => {
    const warning = "缺少 2 张图片，文字和当前可用图片已写入；缺失图片未同步，请在腾讯文档中核对并手动补图";
    const run = {
      groupId: "a",
      groupName: "销售群",
      status: "success" as const,
      dayDir: "D:/a",
      outputs: {},
      smartSheetSync: {
        mode: "automatic" as const,
        status: "success" as const,
        synced: 3,
        missingImages: 2,
        warning,
      },
    };
    const result = {
      status: "success" as const,
      successCount: 1,
      failedCount: 0,
      runs: [run],
    };

    expect(executionHistoryRunDetail(run)).toContain(warning);
    expect(automaticSyncWarningSummary(result)).toEqual({
      groupCount: 1,
      missingImages: 2,
    });
    expect(executionHistoryStatusLabel(item({ result }))).toBe("执行成功");
  });

  it("rejects stale history responses after a newer page request", () => {
    const firstRequest = 1;
    const currentRequest = 2;

    expect(isCurrentHistoryRequest(firstRequest, currentRequest)).toBe(false);
    expect(isCurrentHistoryRequest(currentRequest, currentRequest)).toBe(true);
  });
});

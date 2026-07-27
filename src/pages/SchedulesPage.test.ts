import { describe, expect, it } from "vitest";
import type { PendingScheduleSync, ScheduleExecutionHistoryItem } from "../types";
import {
  createPendingSyncBatch,
  executionHistoryCounts,
  executionHistoryStatusLabel,
  isCurrentConfirmationRequest,
  isCurrentHistoryRequest,
  orphanPendingSyncGroupsFrom,
  withoutPendingSyncBatch,
} from "./SchedulesPage";

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

  it("rejects stale history responses after a newer page request", () => {
    const firstRequest = 1;
    const currentRequest = 2;

    expect(isCurrentHistoryRequest(firstRequest, currentRequest)).toBe(false);
    expect(isCurrentHistoryRequest(currentRequest, currentRequest)).toBe(true);
  });
});

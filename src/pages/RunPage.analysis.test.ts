import { describe, expect, it } from "vitest";
import type { TaskRunResult } from "../types";
import {
  analysisResultLabel,
  canOpenRunDirectory,
  runCompletionNotice,
} from "./RunPage";

const range = {
  startDate: "2026-07-26",
  endDate: "2026-07-26",
  startTime: "00:00",
  endTime: "23:59",
};

const run = (
  groupName: string,
  status: TaskRunResult["status"],
  patch: Partial<TaskRunResult> = {},
): TaskRunResult => ({
  groupId: groupName,
  groupName,
  status,
  error: "",
  dayDir: `D:/exports/${groupName}`,
  outputs: {},
  ...patch,
});

describe("analysisResultLabel", () => {
  it("distinguishes a genuine empty analysis from a missing issue count", () => {
    expect(analysisResultLabel({ issueCount: 0 })).toBe("模型未识别到问题");
    expect(analysisResultLabel({ issueCount: 3 })).toBe("识别 3 个问题");
    expect(analysisResultLabel({})).toBeNull();
  });
});

describe("runCompletionNotice", () => {
  it("distinguishes failed, empty, partial, and mixed-empty runs", () => {
    const failed = runCompletionNotice(
      { status: "failed" },
      [run("销售群", "failed", { error: "大模型请求失败 HTTP 500: {\"trace\":\"hidden\"}" })],
      range,
      1,
    );
    expect(failed).toEqual({
      tone: "error",
      title: "所有群聊处理失败",
      description: "销售群：模型服务暂时不可用，请稍后重试。",
    });

    const empty = runCompletionNotice(
      { status: "empty" },
      [run("销售群", "empty"), run("客服群", "empty")],
      range,
      2,
    );
    expect(empty.title).toBe("所选群聊均无聊天记录");
    expect(empty.description).toContain("2026-07-26 00:00 至 2026-07-26 23:59");
    expect(empty.description).not.toContain("其余群聊");

    expect(runCompletionNotice(
      { status: "partial" },
      [run("产品群", "success"), run("销售群", "empty"), run("客服群", "failed")],
      range,
      3,
    )).toEqual({
      tone: "warning",
      title: "任务部分完成",
      description: "1 个群成功，1 个群无记录，1 个群失败。",
    });

    expect(runCompletionNotice(
      { status: "success" },
      [run("产品群", "success"), run("销售群", "empty")],
      range,
      2,
    )).toEqual({
      tone: "warning",
      title: "处理完成，部分群聊没有记录",
      description: "销售群 已跳过，其余群聊已正常处理。",
    });
  });

  it("distinguishes a genuine empty issue list from a normal success", () => {
    expect(runCompletionNotice(
      { status: "success" },
      [run("产品群", "success", { issueCount: 0 })],
      range,
      1,
    )).toEqual({
      tone: "warning",
      title: "分析完成，但模型未识别到问题",
      description: "产品群 的聊天记录已正常导出，问题清单为空。",
    });

    expect(runCompletionNotice(
      { status: "success" },
      [run("产品群", "success", { issueCount: 2 })],
      range,
      1,
    )).toEqual({ tone: "success", title: "1 个群聊处理完成" });
  });
});

describe("canOpenRunDirectory", () => {
  it("keeps real local exports accessible after a later preview failure", () => {
    expect(canOpenRunDirectory(run("产品群", "success"))).toBe(true);
    expect(canOpenRunDirectory(run("空群", "empty"))).toBe(false);
    expect(canOpenRunDirectory(run("失败群", "failed", { dayDir: "" }))).toBe(false);
    expect(canOpenRunDirectory(run("预览失败群", "failed", {
      outputs: { xlsx: "D:/exports/preview-failed/issues.xlsx" },
    }))).toBe(true);
  });
});

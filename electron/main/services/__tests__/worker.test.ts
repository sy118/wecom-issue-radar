import { describe, expect, it } from "vitest";

import {
  describeWorkerFailure,
  frozenTaskTemplateId,
  smartSheetSyncGuard,
  workerLaunchErrorMessage,
} from "../worker";

describe("worker failure messages", () => {
  const emptyOutcome = { result: undefined, reportedError: null, logs: [] };

  it("prefers the error reported over NDJSON", () => {
    expect(
      describeWorkerFailure({
        outcome: {
          result: undefined,
          reportedError: "配置不是有效 JSON\nTraceback ...",
          logs: [],
        },
        exitCode: 1,
        stderr: "ignored stderr",
      }),
    ).toBe("配置不是有效 JSON\nTraceback ...");
  });

  it("reports a missing result when the worker exits cleanly", () => {
    expect(
      describeWorkerFailure({ outcome: emptyOutcome, exitCode: 0, stderr: "" }),
    ).toBe("处理引擎没有返回结果");
  });

  it("reports the exit status when stderr is empty", () => {
    expect(
      describeWorkerFailure({ outcome: emptyOutcome, exitCode: 3, stderr: "  " }),
    ).toBe("处理引擎退出，状态码：3");
  });

  it("includes stderr detail when the worker crashes", () => {
    expect(
      describeWorkerFailure({
        outcome: emptyOutcome,
        exitCode: 1,
        stderr: "ModuleNotFoundError: No module named 'langchain'\n",
      }),
    ).toBe(
      "处理引擎执行失败：ModuleNotFoundError: No module named 'langchain'",
    );
  });

  it("accepts a successful result without a failure message", () => {
    expect(
      describeWorkerFailure({
        outcome: { result: { groups: [] }, reportedError: null, logs: [] },
        exitCode: 0,
        stderr: "",
      }),
    ).toBeNull();
  });
});

describe("worker launch errors", () => {
  it("turns ENOENT into an actionable engine-not-found message", () => {
    const message = workerLaunchErrorMessage("C:\\app\\worker.exe", {
      code: "ENOENT",
      message: "spawn C:\\app\\worker.exe ENOENT",
    });
    expect(message).toContain("找不到处理引擎");
    expect(message).toContain("WECOM_ISSUE_RADAR_WORKER");
  });

  it("keeps the underlying reason for other spawn failures", () => {
    const message = workerLaunchErrorMessage("python.exe", {
      code: "EACCES",
      message: "permission denied",
    });
    expect(message).toContain("无法启动处理引擎");
    expect(message).toContain("permission denied");
  });
});

describe("Smart Sheet sync guard", () => {
  const payload = {
    definitionPath: " D:/exports/issues.json ",
    expectedTemplateRevision: " template-r1 ",
    expectedDocumentRevision: " document-r2 ",
  };

  it("trims and accepts a complete frozen snapshot", () => {
    expect(smartSheetSyncGuard(payload)).toEqual({
      definitionPath: "D:/exports/issues.json",
      expectedTemplateRevision: "template-r1",
      expectedDocumentRevision: "document-r2",
    });
  });

  it("fails closed when any guard field is blank", () => {
    for (const key of [
      "definitionPath",
      "expectedTemplateRevision",
      "expectedDocumentRevision",
    ]) {
      expect(() =>
        smartSheetSyncGuard({ ...payload, [key]: "   " }),
      ).toThrow();
    }
  });
});

describe("frozen task template id", () => {
  it("requires a frozen template only when preparing a Smart Sheet", () => {
    expect(() =>
      frozenTaskTemplateId({ prepareSmartSheet: true, smartSheetTemplateId: "  " }),
    ).toThrow("必须冻结模板 ID");
    expect(
      frozenTaskTemplateId({
        prepareSmartSheet: true,
        smartSheetTemplateId: " template-a ",
      }),
    ).toBe("template-a");
    expect(frozenTaskTemplateId({ prepareSmartSheet: false })).toBe("");
  });
});

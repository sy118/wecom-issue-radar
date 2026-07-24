import { describe, expect, it } from "vitest";
import type { TaskRunResult } from "../types";
import { smartSheetConfigurationBlockers } from "./smartSheetSync";

function runWithPreview(preview: TaskRunResult["smartSheetPreview"]): TaskRunResult {
  return {
    groupId: "group-1",
    groupName: "产品群",
    dayDir: "D:/exports/product",
    outputs: {},
    smartSheetPreview: preview,
  };
}

describe("smartSheetConfigurationBlockers", () => {
  it("treats invalid mappings and a missing webhook as write blockers", () => {
    expect(smartSheetConfigurationBlockers([
      runWithPreview({
        pending: 7,
        already_synced: 0,
        mapping_valid: false,
        validation_error: "问题分类枚举不匹配",
        webhook_configured: false,
      }),
    ])).toEqual([
      "问题分类枚举不匹配",
      "产品群 尚未配置写入 Webhook",
    ]);
  });
});

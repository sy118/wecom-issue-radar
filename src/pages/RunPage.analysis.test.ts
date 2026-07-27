import { describe, expect, it } from "vitest";
import { analysisResultLabel } from "./RunPage";

describe("analysisResultLabel", () => {
  it("distinguishes a genuine empty analysis from a missing issue count", () => {
    expect(analysisResultLabel({ issueCount: 0 })).toBe("模型未识别到问题");
    expect(analysisResultLabel({ issueCount: 3 })).toBe("识别 3 个问题");
    expect(analysisResultLabel({})).toBeNull();
  });
});

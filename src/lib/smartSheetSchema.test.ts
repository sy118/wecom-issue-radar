import { describe, expect, it } from "vitest";
import { convertSmartSheetExampleData } from "./smartSheetSchema";

describe("convertSmartSheetExampleData", () => {
  it("extracts the schema from a complete Tencent example payload", () => {
    const result = convertSmartSheetExampleData(JSON.stringify({
      schema: {
        f_text: { title: "文本", type: "text" },
        f_select: { title: "单选", type: "single_select", enum: ["待处理", "已完成"] },
        f_user: { title: "人员", type: "user" },
      },
      add_records: [{ values: { f_text: "测试文本" } }],
    }));

    expect(result.schema).toEqual({
      f_text: { title: "文本", type: "text" },
      f_select: { title: "单选", type: "single_select", enum: ["待处理", "已完成"] },
      f_user: { title: "人员", type: "user" },
    });
    expect(result.unsupportedTypes).toEqual(["user"]);
  });

  it("also accepts a plain schema object", () => {
    expect(convertSmartSheetExampleData(JSON.stringify({
      f_date: { title: " 日期 ", type: " date_time " },
    })).schema).toEqual({
      f_date: { title: "日期", type: "date_time" },
    });
  });

  it("rejects malformed or incomplete examples with actionable messages", () => {
    expect(() => convertSmartSheetExampleData("not json")).toThrow("示例数据不是有效 JSON");
    expect(() => convertSmartSheetExampleData(JSON.stringify({ schema: {} }))).toThrow("schema 不能为空");
    expect(() => convertSmartSheetExampleData(JSON.stringify({
      schema: { f_select: { title: "单选", type: "single_select", enum: [1] } },
    }))).toThrow("enum 必须是字符串数组");
  });
});

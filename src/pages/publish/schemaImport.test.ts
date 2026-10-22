import { describe, expect, it } from "vitest";
import {
  exportSchemaAsGenericJson,
  exportSchemaAsTencentFields,
  mapTencentFieldType,
  normalizeFieldKey,
  parseTencentSchema,
  schemaTypeLabel,
} from "./schemaImport";
import { applySchema } from "./logic";
import type { TencentDraft } from "./types";

const internalSchema = {
  f_desc: { title: "*问题描述", type: "text" },
  f_status: { title: "状态", type: "single_select", enum: ["待处理", "已完成"] },
};

describe("parseTencentSchema shapes", () => {
  it("parses the internal object shape", () => {
    const result = parseTencentSchema(JSON.stringify(internalSchema));
    expect(result.schema).toEqual(internalSchema);
    expect(result.fields.map((field) => field.fieldId)).toEqual(["f_desc", "f_status"]);
    expect(result.fields[0].required).toBe(true);
    expect(result.typeCounts).toEqual({ text: 1, single_select: 1 });
    expect(result.problems).toEqual([]);
  });

  it("parses a direct field array with alias keys", () => {
    const result = parseTencentSchema(JSON.stringify([
      { field_id: "f1", field_title: "问题", field_type: "单行文本" },
      { fieldId: "f2", name: "补充说明", fieldType: "多行文本", required: true },
      { id: "f3", title: "附件", type: "attachment" },
      { key: "f4", label: "链接", type: "url" },
    ]));
    expect(result.fields.map((field) => field.fieldId)).toEqual(["f1", "f2", "f3", "f4"]);
    expect(result.fields.map((field) => field.type)).toEqual(["text", "text", "text", "text"]);
    expect(result.fields[1].title).toBe("*补充说明");
    expect(result.fields[1].required).toBe(true);
    expect(result.notes.some((note) => note.includes("图片/附件"))).toBe(true);
    expect(result.notes.some((note) => note.includes("链接"))).toBe(true);
  });

  it("parses every common wrapper", () => {
    const fields = [{ field_id: "f1", field_title: "问题描述", field_type: "text" }];
    for (const wrapper of ["fields", "schema", "columns", "data"]) {
      const result = parseTencentSchema(JSON.stringify({ [wrapper]: fields }));
      expect(result.fields).toHaveLength(1);
      expect(result.schema.f1.title).toBe("问题描述");
    }
    expect(parseTencentSchema(JSON.stringify({ fields: { f1: { title: "字段", type: "text" } } })).schema.f1).toBeDefined();
    expect(parseTencentSchema(JSON.stringify({ fields: { f1: "文本列" } })).schema.f1.title).toBe("文本列");
    expect(parseTencentSchema(JSON.stringify({ data: { columns: fields } })).fields[0].fieldId).toBe("f1");
    expect(parseTencentSchema(JSON.stringify({ schema: { f1: { title: "字段", type: "text" } } })).schema.f1).toBeDefined();
  });

  it("parses primitive title arrays as text columns", () => {
    const result = parseTencentSchema(JSON.stringify(["问题描述", "处理人"]));
    expect(result.fields.map((field) => field.title)).toEqual(["问题描述", "处理人"]);
    expect(result.fields.map((field) => field.fieldId)).toEqual(["wenti_miaoshu", "chuliren"]);
  });

  it("parses a single field object", () => {
    const result = parseTencentSchema(JSON.stringify({ field_id: "f1", field_title: "问题", field_type: "text" }));
    expect(result.fields).toHaveLength(1);
    expect(result.schema).toEqual({ f1: { title: "问题", type: "text" } });
  });
});

describe("type mapping", () => {
  const cases: Array<[string, string, string]> = [
    ["text", "text", "text"],
    ["string", "text", "text"],
    ["单行文本", "text", "text"],
    ["long_text", "long_text", "text"],
    ["多行文本", "long_text", "text"],
    ["number", "number", "number"],
    ["数字", "number", "number"],
    ["single_select", "single_select", "single_select"],
    ["单选", "single_select", "single_select"],
    ["multiple_select", "multiple_select", "multiple_select"],
    ["多选", "multiple_select", "multiple_select"],
    ["checkbox", "boolean", "checkbox"],
    ["boolean", "boolean", "checkbox"],
    ["是", "boolean", "checkbox"],
    ["date", "date", "date_time"],
    ["日期", "date", "date_time"],
    ["date_time", "date", "date_time"],
    ["image", "image", "text"],
    ["attachment", "image", "text"],
    ["附件", "image", "text"],
    ["url", "url", "text"],
    ["link", "url", "text"],
    ["FIELD_TYPE_TEXT", "text", "text"],
    ["text（单行）", "text", "text"],
    ["日期类型", "date", "date_time"],
    ["是/否", "boolean", "checkbox"],
  ];

  it.each(cases)("maps %s to semantic %s / internal %s", (raw, semantic, internal) => {
    const mapping = mapTencentFieldType(raw);
    expect(mapping.semantic).toBe(semantic);
    expect(mapping.internal).toBe(internal);
  });

  it("marks downgraded and unknown types", () => {
    expect(mapTencentFieldType("附件").note).toContain("文本");
    expect(mapTencentFieldType("url").note).toContain("文本");
    expect(mapTencentFieldType("多行文本").note).toContain("文本");
    expect(mapTencentFieldType("日期").note).toContain("date_time");
    expect(mapTencentFieldType("user").semantic).toBe("unknown");
    expect(mapTencentFieldType("user").internal).toBe("text");
    expect(mapTencentFieldType(42).semantic).toBe("unknown");
    expect(mapTencentFieldType(undefined).note).toContain("缺少字段类型");
  });

  it("exposes a human readable type label", () => {
    expect(schemaTypeLabel("single_select")).toBe("单选");
    expect(schemaTypeLabel("custom_unknown")).toBe("custom_unknown");
  });
});

describe("options and required", () => {
  it("accepts string and object option arrays", () => {
    const result = parseTencentSchema(JSON.stringify({
      fields: [
        { field_id: "f1", field_title: "状态", field_type: "单选", options: ["待处理", "已完成"] },
        { field_id: "f2", field_title: "分类", field_type: "多选", choices: [{ name: "A" }, { title: "B" }, { value: "C" }, { value: 1 }] },
        { field_id: "f3", field_title: "来源", field_type: "单选", property: { options: [{ name: "群聊" }, "私聊"] } },
      ],
    }));
    expect(result.schema.f1.enum).toEqual(["待处理", "已完成"]);
    expect(result.schema.f2.enum).toEqual(["A", "B", "C", "1"]);
    expect(result.schema.f3.enum).toEqual(["群聊", "私聊"]);
  });

  it("derives required from booleans, strings or a leading star", () => {
    const result = parseTencentSchema(JSON.stringify([
      { field_id: "a", title: "必须", type: "text", required: true },
      { field_id: "b", title: "*星号必填", type: "text" },
      { field_id: "c", title: "可选", type: "text", required: false },
      { field_id: "d", title: "字符串必填", type: "text", required: "是" },
    ]));
    expect(result.fields.map((field) => field.title)).toEqual(["*必须", "*星号必填", "可选", "*字符串必填"]);
    expect(result.fields.map((field) => field.required)).toEqual([true, true, false, true]);
  });
});

describe("ids and problems", () => {
  it("renames duplicate ids and keeps them unique", () => {
    const result = parseTencentSchema(JSON.stringify([
      { field_id: "dup", title: "第一列", type: "text" },
      { field_id: "dup", title: "第二列", type: "text" },
    ]));
    expect(Object.keys(result.schema)).toEqual(["dup", "dup_2"]);
    expect(result.problems.some((problem) => problem.message.includes("重复"))).toBe(true);
  });

  it("falls back to normalized keys and reports empty titles", () => {
    const result = parseTencentSchema(JSON.stringify([
      { field_title: "Order No", field_type: "text" },
      { field_id: "no_title", field_type: "text" },
      { field_title: "问题描述", field_type: "text" },
    ]));
    expect(Object.keys(result.schema)).toEqual(["order_no", "no_title", "wenti_miaoshu"]);
    expect(result.problems.some((problem) => problem.message.includes("缺少标题"))).toBe(true);
  });

  it("normalizes field keys with a built-in pinyin glossary", () => {
    expect(normalizeFieldKey("Order No")).toBe("order_no");
    expect(normalizeFieldKey("  F-1  ")).toBe("f_1");
    expect(normalizeFieldKey("123")).toBe("field_123");
    expect(normalizeFieldKey("问题描述")).toBe("wenti_miaoshu");
    expect(normalizeFieldKey("影响范围")).toBe("yingxiangfanwei");
    expect(normalizeFieldKey("未知短语")).toBe("");
  });

  it("warns once for unknown types and keeps the original type for preview", () => {
    const result = parseTencentSchema(JSON.stringify([
      { field_id: "u1", title: "人员", type: "user" },
      { field_id: "u2", title: "负责人", type: "人员" },
    ]));
    expect(result.fields.map((field) => field.type)).toEqual(["text", "text"]);
    expect(result.fields[0].originalType).toBe("user");
    const unknownWarnings = result.problems.filter((problem) => problem.message.includes("无法识别"));
    expect(unknownWarnings).toHaveLength(2);
  });
});

describe("errors", () => {
  it("rejects empty, invalid and unsupported inputs with readable messages", () => {
    expect(() => parseTencentSchema("")).toThrow("请先粘贴");
    expect(() => parseTencentSchema("not json")).toThrow("不是有效 JSON");
    expect(() => parseTencentSchema("[]")).toThrow("没有识别到任何字段");
    expect(() => parseTencentSchema("{}")).toThrow("没有识别到字段定义");
    expect(() => parseTencentSchema('{"fields": []}')).toThrow("没有识别到任何字段");
    expect(() => parseTencentSchema('{"fields": "bad"}')).toThrow("必须是字段数组或对象");
    expect(() => parseTencentSchema('"just a string"')).toThrow("必须是字段数组或对象");
  });
});

describe("copy helpers", () => {
  it("round-trips the internal schema through generic JSON", () => {
    const result = parseTencentSchema(exportSchemaAsGenericJson(internalSchema));
    expect(result.schema).toEqual(internalSchema);
  });

  it("round-trips the internal schema through the Tencent field array", () => {
    const schema = {
      f_text: { title: "*问题描述", type: "text" },
      f_count: { title: "数量", type: "number" },
      f_date: { title: "日期", type: "date_time" },
      f_check: { title: "已处理", type: "checkbox" },
      f_select: { title: "状态", type: "single_select", enum: ["A", "B"] },
    };
    const exported = JSON.parse(exportSchemaAsTencentFields(schema));
    expect(exported.fields[0]).toEqual({
      field_id: "f_text",
      field_title: "问题描述",
      field_type: "text",
      required: true,
    });
    const result = parseTencentSchema(exportSchemaAsTencentFields(schema));
    expect(result.schema).toEqual(schema);
  });
});

describe("applying an imported schema", () => {
  it("keeps matching mappings and drops orphaned ones", () => {
    const draft: TencentDraft = {
      template_id: "default",
      url: "",
      webhook_url: "",
      webhook_url_env: "",
      batch_size: 50,
      auto_sync: false,
      prompt_id: "prompt-a",
      schema: {
        f_desc: { title: "*问题描述", type: "text" },
        f_old: { title: "旧列", type: "text" },
      },
      field_mappings: [
        { source_key: "problem_description", target_field_id: "f_desc", target_type: "text", required: true, default_value: "" },
        { source_key: "order_no", target_field_id: "f_old", target_type: "text", required: false, default_value: "" },
      ],
      transforms: { f_old: { mode: "constant", constant: "x" } },
    };
    const imported = parseTencentSchema(JSON.stringify({
      fields: [{ field_id: "f_desc", field_title: "*问题描述", field_type: "text" }],
    }));
    const next = applySchema(draft, imported.schema);
    expect(Object.keys(next.schema)).toEqual(["f_desc"]);
    expect(next.field_mappings.map((mapping) => mapping.target_field_id)).toEqual(["f_desc"]);
    expect(next.transforms).toEqual({});
  });
});

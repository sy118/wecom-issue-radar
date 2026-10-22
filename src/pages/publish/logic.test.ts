import { describe, expect, it } from "vitest";
import type {
  AppConfig,
  IssueFieldDefinition,
  IssueFieldType,
  PublishTarget,
  SmartSheetConfig,
} from "../../types";
import {
  TRANSFORM_MODES,
  addMapping,
  addSchemaField,
  applySchema,
  buildPublishSave,
  buildSampleIssue,
  clampBatchSize,
  defaultIssueFields,
  duplicateTemplateIds,
  enumMapText,
  errorCount,
  formatDateValue,
  importLegacyTemplate,
  invalidSourceIndexes,
  isValidDateFormat,
  issueAnalysisPromptOptions,
  mappingDefaultText,
  mappingSourceOptions,
  normalizeUpload,
  parseEnumMap,
  parseMappingDefault,
  parseSchemaJson,
  previewDraft,
  readTencentDraft,
  removeMapping,
  removeSchemaField,
  renameSchemaField,
  resolvePromptId,
  setTransform,
  sourceLabel,
  targetConfigFromDraft,
  targetsFromConfig,
  templatePlaceholders,
  updateMapping,
  updateSchemaField,
  updateSchemaFieldRequired,
  validateDraft,
} from "./logic";
import type { TencentDraft, UploadDraft } from "./types";

const issueField = (
  key: string,
  label: string,
  type: IssueFieldType = "text",
  options: string[] = [],
): IssueFieldDefinition => ({
  key,
  label,
  type,
  required: false,
  instruction: "",
  options,
  default_value: "",
});

const upload = (overrides: Partial<UploadDraft> = {}): UploadDraft => ({
  token_endpoint: "",
  image_upload_endpoint: "",
  image_form_field: "media",
  propagation_wait_ms_after_uploads: 1500,
  delay_ms_between_image_uploads: 120,
  corpid: "",
  corpsecret: "",
  ...overrides,
});

const legacyTemplate = (overrides: Partial<SmartSheetConfig["templates"][number]> = {}) => ({
  id: "default",
  name: "旧版默认模板",
  url: "https://docs.qq.com/smartsheet/demo",
  webhook_url_env: "WECOM_SMARTSHEET_WEBHOOK_URL",
  webhook_url: "",
  batch_size: 50,
  schema: {
    f_desc: { title: "*问题描述", type: "text" },
    f_order: { title: "订单号", type: "text" },
  },
  field_mappings: [
    { source_key: "problem_description", target_field_id: "f_desc", target_type: "text", required: true, default_value: "" },
  ],
  ...overrides,
});

function baseConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  const smartSheet: SmartSheetConfig = {
    default_template_id: "default",
    templates: [legacyTemplate()],
    upload: upload(),
  };
  const config: AppConfig = {
    wxwork_db_dir: "",
    wxwork_keys_file: "",
    target_group_id: "",
    target_group_name: "",
    default_workspace: "D:/exports",
    timezone: "Asia/Shanghai",
    ocr: { base_url: "", api_key: "", model: "" },
    llm: { base_url: "http://localhost:11434/v1", api_key: "key", model: "model" },
    prompts: {
      default_id: "prompt-a",
      items: [
        {
          id: "prompt-a",
          name: "问题分析提示词",
          description: "",
          content: "",
          issue_fields: [issueField("problem_description", "问题描述", "long_text"), issueField("order_no", "订单号")],
        },
        {
          id: "prompt-reply",
          name: "群回复提示词",
          description: "",
          content: "",
          issue_fields: [],
          scene: "reply",
        },
      ],
    },
    ai_strategies: [
      {
        id: "strategy-issue",
        name: "问题分析策略",
        description: "",
        scene: "issue_analysis",
        content: "",
        issue_fields: [issueField("problem_description", "问题描述", "long_text"), issueField("order_no", "订单号")],
      },
      {
        id: "strategy-alt",
        name: "精简问题策略",
        description: "",
        scene: "issue_analysis",
        content: "",
        issue_fields: [issueField("problem_description", "问题描述", "long_text")],
      },
      {
        id: "strategy-reply",
        name: "群回复策略",
        description: "",
        scene: "reply",
        content: "",
        issue_fields: [],
      },
    ],
    smart_sheet: smartSheet,
  };
  return { ...config, ...overrides };
}

const baseSchema = () => ({
  f_desc: { title: "*问题描述", type: "text" },
  f_order: { title: "订单号", type: "text" },
});

function tencentDraft(overrides: Partial<TencentDraft> = {}): TencentDraft {
  return {
    template_id: "default",
    url: "https://docs.qq.com/smartsheet/demo",
    webhook_url: "",
    webhook_url_env: "",
    batch_size: 50,
    auto_sync: false,
    prompt_id: "strategy-issue",
    schema: baseSchema(),
    field_mappings: [
      { source_key: "problem_description", target_field_id: "f_desc", target_type: "text", required: true, default_value: "" },
      { source_key: "order_no", target_field_id: "f_order", target_type: "text", required: false, default_value: "" },
    ],
    transforms: {},
    ...overrides,
  };
}

function tencentTarget(overrides: Partial<PublishTarget> = {}): PublishTarget {
  return {
    id: "tencent_default",
    name: "腾讯文档",
    kind: "tencent_smartsheet",
    config: {},
    ...overrides,
  };
}

describe("prompt linking", () => {
  it("prefers issue-analysis strategies and ignores reply-scene strategies", () => {
    const options = issueAnalysisPromptOptions(baseConfig());
    expect(options.map((option) => option.id)).toEqual([
      "strategy-issue",
      "strategy-alt",
      "prompt-a",
    ]);
    expect(options[0].strategy).toBe(true);
    expect(options[2].strategy).toBe(false);
    expect(options[0].fields.map((field) => field.key)).toEqual(["problem_description", "order_no"]);
  });

  it("falls back to legacy prompt items when ai_strategies is missing", () => {
    const options = issueAnalysisPromptOptions(baseConfig({ ai_strategies: [] }));
    expect(options.map((option) => option.id)).toEqual(["prompt-a"]);
    expect(options[0].name).toBe("问题分析提示词");
  });

  it("synthesizes a default option and resolves unknown selections", () => {
    const empty = baseConfig({
      ai_strategies: [],
      prompts: { default_id: "", items: [] },
    });
    const options = issueAnalysisPromptOptions(empty);
    expect(options).toHaveLength(1);
    expect(options[0].name).toBe("默认问题字段");
    expect(defaultIssueFields(empty).length).toBeGreaterThan(0);
    expect(resolvePromptId(empty, "does-not-exist")).toBe("");
    expect(resolvePromptId(baseConfig(), "does-not-exist")).toBe("prompt-a");
    expect(resolvePromptId(baseConfig(), "strategy-issue")).toBe("strategy-issue");
  });

  it("derives source options from system variables plus the selected prompt fields", () => {
    const sources = mappingSourceOptions(baseConfig(), "strategy-issue");
    expect(sources.slice(0, 5).map((source) => source.key)).toEqual([
      "$date",
      "$images",
      "$sender",
      "$message_time",
      "$issue_key",
    ]);
    expect(sources.slice(0, 5).every((source) => source.system)).toBe(true);
    expect(sources.map((source) => source.key)).toContain("problem_description");
    expect(sourceLabel(sources, "problem_description")).toBe("问题描述");
    expect(sources.find((source) => source.key === "order_no")?.type).toBe("text");
  });

  it("keeps mappings on prompt switch and marks only the invalid sources", () => {
    const config = baseConfig();
    const sources = mappingSourceOptions(config, "strategy-alt");
    const draft = tencentDraft({ prompt_id: "strategy-alt" });
    expect(invalidSourceIndexes(draft.field_mappings, sources)).toEqual([1]);
    const problems = validateDraft(draft, sources);
    expect(problems.filter((problem) => problem.level === "warning").some(
      (problem) => problem.message.includes("不在当前提示词中"),
    )).toBe(true);
    expect(errorCount(problems)).toBe(0);
    const restored = invalidSourceIndexes(
      draft.field_mappings,
      mappingSourceOptions(config, "strategy-issue"),
    );
    expect(restored).toEqual([]);
  });
});

describe("draft normalization", () => {
  it("expands legacy smart_sheet templates into targets without losing schema", () => {
    const config = baseConfig({ publish_targets: undefined });
    const targets = targetsFromConfig(config);
    expect(targets.map((target) => target.kind)).toEqual([
      "excel",
      "markdown",
      "tencent_smartsheet",
    ]);
    const draft = readTencentDraft(config, targets[2]);
    expect(draft.template_id).toBe("default");
    expect(Object.keys(draft.schema)).toEqual(["f_desc", "f_order"]);
    expect(draft.field_mappings).toHaveLength(1);
    expect(draft.prompt_id).toBe("prompt-a");
  });

  it("derives the template id from legacy target ids", () => {
    const config = baseConfig();
    expect(readTencentDraft(config, tencentTarget({ id: "tencent:default" })).template_id).toBe("default");
    expect(readTencentDraft(config, tencentTarget({ id: "tencent_default" })).template_id).toBe("default");
    expect(readTencentDraft(config, tencentTarget({ id: "default" })).template_id).toBe("default");
    expect(readTencentDraft(config, tencentTarget({
      id: "custom",
      config: { template_id: "explicit" },
    })).template_id).toBe("explicit");
  });

  it("clamps batch size and tolerates missing fields", () => {
    const config = baseConfig();
    const target = tencentTarget({
      config: { batch_size: 500, schema: { f: { title: "列", type: "text" } }, field_mappings: [{ source_key: "order_no", target_field_id: "f" }] },
    });
    const draft = readTencentDraft(config, target);
    expect(draft.batch_size).toBe(50);
    expect(clampBatchSize(0)).toBe(1);
    expect(clampBatchSize("bad")).toBe(50);
    expect(draft.transforms).toEqual({});
    expect(draft.field_mappings[0].target_type).toBe("text");
  });

  it("canonicalizes mappings and preserves unknown config keys on save", () => {
    const draft = tencentDraft({
      field_mappings: [
        { source_key: "problem_description", target_field_id: "f_desc", target_type: "number", required: true, default_value: "" },
        { source_key: "order_no", target_field_id: "f_order", target_type: "text", required: false, default_value: "" },
      ],
      transforms: {
        f_desc: { mode: "constant", constant: "固定描述" },
        removed_field: { mode: "date", date_format: "YYYY" },
      },
    });
    const saved = targetConfigFromDraft(
      draft,
      { template_id: "old", unknown_key: "keep-me" },
      baseConfig(),
      "tencent_default",
    );
    const mappings = saved.field_mappings as Array<{ target_type: string; required: boolean }>;
    const transforms = saved.transforms as Record<string, { mode: string }>;
    expect(mappings[0].target_type).toBe("text");
    expect(mappings[0].required).toBe(true);
    expect(saved.unknown_key).toBe("keep-me");
    expect(transforms.f_desc.mode).toBe("constant");
    expect(transforms.removed_field).toBeUndefined();
  });
});

describe("validation", () => {
  it("accepts a fully mapped draft", () => {
    expect(validateDraft(tencentDraft(), mappingSourceOptions(baseConfig(), "strategy-issue"))).toEqual([]);
  });

  it("reports duplicate target mappings", () => {
    const draft = tencentDraft({
      field_mappings: [
        { source_key: "problem_description", target_field_id: "f_desc", target_type: "text", required: true, default_value: "" },
        { source_key: "order_no", target_field_id: "f_desc", target_type: "text", required: false, default_value: "" },
      ],
    });
    const problems = validateDraft(draft, mappingSourceOptions(baseConfig(), "strategy-issue"));
    expect(problems.some((problem) => problem.message.includes("重复映射"))).toBe(true);
    expect(errorCount(problems)).toBeGreaterThan(0);
  });

  it("reports missing targets, wrong types and missing required mappings", () => {
    const sources = mappingSourceOptions(baseConfig(), "strategy-issue");
    const missingTarget = validateDraft(tencentDraft({
      field_mappings: [{ source_key: "problem_description", target_field_id: "ghost", target_type: "text", required: false, default_value: "" }],
    }), sources);
    expect(missingTarget.some((problem) => problem.message.includes("目标字段不存在"))).toBe(true);

    const wrongType = validateDraft(tencentDraft({
      field_mappings: [
        { source_key: "problem_description", target_field_id: "f_desc", target_type: "number", required: true, default_value: "" },
        { source_key: "order_no", target_field_id: "f_order", target_type: "text", required: false, default_value: "" },
      ],
    }), sources);
    expect(wrongType.some((problem) => problem.message.includes("映射类型应为 text"))).toBe(true);

    const noRequiredMapping = validateDraft(tencentDraft({
      field_mappings: [{ source_key: "order_no", target_field_id: "f_order", target_type: "text", required: false, default_value: "" }],
    }), sources);
    expect(noRequiredMapping.some((problem) => problem.message.includes("缺少必填目标字段映射"))).toBe(true);
  });

  it("requires a value for required mappings and warns on expired sources", () => {
    const draft = tencentDraft({
      field_mappings: [
        { source_key: "expired_key", target_field_id: "f_desc", target_type: "text", required: true, default_value: "" },
        { source_key: "order_no", target_field_id: "f_order", target_type: "text", required: false, default_value: "" },
      ],
    });
    const problems = validateDraft(draft, mappingSourceOptions(baseConfig(), "strategy-issue"));
    expect(problems.some((problem) => problem.message.includes("必填字段"))).toBe(true);
    expect(problems.some((problem) => problem.level === "warning")).toBe(true);

    const withDefault = validateDraft(tencentDraft({
      field_mappings: [
        { source_key: "expired_key", target_field_id: "f_desc", target_type: "text", required: true, default_value: "兜底描述" },
        { source_key: "order_no", target_field_id: "f_order", target_type: "text", required: false, default_value: "" },
      ],
    }), mappingSourceOptions(baseConfig(), "strategy-issue"));
    expect(withDefault.some((problem) => problem.message.includes("必填字段"))).toBe(false);
  });

  it("reports empty schemas and empty mapping lists", () => {
    const sources = mappingSourceOptions(baseConfig(), "strategy-issue");
    const emptySchema = validateDraft(tencentDraft({ schema: {}, field_mappings: [] }), sources);
    expect(emptySchema.some((problem) => problem.message.includes("尚未定义目标字段 Schema"))).toBe(true);
  });

  it("warns for unsupported types and missing select options", () => {
    const sources = mappingSourceOptions(baseConfig(), "strategy-issue");
    const problems = validateDraft(tencentDraft({
      schema: {
        f_user: { title: "处理人", type: "user" },
        f_select: { title: "状态", type: "single_select" },
      },
      field_mappings: [
        { source_key: "order_no", target_field_id: "f_user", target_type: "user", required: false, default_value: "" },
        { source_key: "order_no", target_field_id: "f_select", target_type: "single_select", required: false, default_value: "" },
      ],
    }), sources);
    expect(problems.some((problem) => problem.message.includes("暂不受"))).toBe(true);
    expect(problems.some((problem) => problem.message.includes("建议补充枚举选项"))).toBe(true);
  });
});

describe("transforms and preview", () => {
  const sources = (config = baseConfig()) => mappingSourceOptions(config, "strategy-issue");

  it("resolves direct values and falls back to the default value", () => {
    const direct = previewDraft(tencentDraft(), sources());
    expect(direct.rows.find((row) => row.fieldId === "f_order")?.display).toBe("示例文本");
    const fallback = previewDraft(tencentDraft({
      field_mappings: [
        { source_key: "problem_description", target_field_id: "f_desc", target_type: "text", required: true, default_value: "" },
        { source_key: "expired_key", target_field_id: "f_order", target_type: "text", required: false, default_value: "默认单号" },
      ],
    }), sources());
    expect(fallback.rows.find((row) => row.fieldId === "f_order")?.display).toBe("默认单号");
  });

  it("supports constant, template, date and enum modes", () => {
    const constant = previewDraft(tencentDraft({
      transforms: { f_order: { mode: "constant", constant: "待评估" } },
    }), sources());
    expect(constant.rows.find((row) => row.fieldId === "f_order")?.display).toBe("待评估");

    const template = previewDraft(tencentDraft({
      transforms: { f_order: { mode: "template", template: "{{order_no}} / {{$date}}" } },
    }), sources());
    expect(template.rows.find((row) => row.fieldId === "f_order")?.display).toContain("示例文本 /");

    const date = previewDraft(tencentDraft({
      field_mappings: [
        { source_key: "problem_description", target_field_id: "f_desc", target_type: "text", required: true, default_value: "" },
        { source_key: "$date", target_field_id: "f_order", target_type: "text", required: false, default_value: "" },
      ],
      transforms: { f_order: { mode: "date", date_format: "YYYY/MM/DD" } },
    }), sources());
    expect(date.rows.find((row) => row.fieldId === "f_order")?.display).toMatch(/^\d{4}\/\d{2}\/\d{2}$/);

    const enumDraft = tencentDraft({
      schema: { ...baseSchema(), f_order: { title: "状态", type: "single_select", enum: ["待处理", "已完成"] } },
      transforms: { f_order: { mode: "enum", enum_map: { "示例文本": "已完成" } } },
    });
    const enumPreview = previewDraft(enumDraft, sources());
    expect(enumPreview.rows.find((row) => row.fieldId === "f_order")?.display).toBe("已完成");
  });

  it("formats dates and validates formats", () => {
    expect(formatDateValue("2026-10-22 09:30:00", "YYYY/MM/DD HH:mm")).toBe("2026/10/22 09:30");
    expect(formatDateValue("2026-10-22", "YYYY-MM-DD")).toBe("2026-10-22");
    expect(formatDateValue("not-a-date", "YYYY")).toBe("not-a-date");
    expect(isValidDateFormat("YYYY-MM-DD")).toBe(true);
    expect(isValidDateFormat("随便")).toBe(false);
    expect(templatePlaceholders("{{a}} {{ b }} {{}}")).toEqual(["a", "b"]);
  });

  it("builds a deterministic sample issue from source types", () => {
    const config = baseConfig();
    const now = new Date("2026-10-22T09:30:00");
    const sample = buildSampleIssue(mappingSourceOptions(config, "strategy-issue"), now);
    expect(sample.$date).toBe("2026-10-22");
    expect(sample.$message_time).toBe("2026-10-22 09:30");
    expect(Array.isArray(sample.$images)).toBe(true);
    expect(sample.problem_description).toContain("示例问题描述");
    expect(sample.order_no).toBe("示例文本");
  });

  it("renders every schema column, including unmapped ones", () => {
    const result = previewDraft(tencentDraft({
      schema: { ...baseSchema(), f_extra: { title: "备注", type: "text" } },
    }), sources());
    expect(result.rows.map((row) => row.fieldId)).toEqual(["f_desc", "f_order", "f_extra"]);
    expect(result.rows[2].mapped).toBe(false);
    expect(result.sample.length).toBe(sources().length);
  });

  it("warns on broken transform parameters", () => {
    const constant = validateDraft(tencentDraft({
      transforms: { f_order: { mode: "constant", constant: "  " } },
    }), sources());
    expect(constant.some((problem) => problem.level === "error" && problem.message.includes("固定值"))).toBe(true);

    const template = validateDraft(tencentDraft({
      transforms: { f_order: { mode: "template", template: "{{ghost}} {{$value}}" } },
    }), sources());
    expect(template.some((problem) => problem.message.includes("{{ghost}}"))).toBe(true);

    const enumDraft = validateDraft(tencentDraft({
      schema: { ...baseSchema(), f_order: { title: "状态", type: "single_select", enum: ["已完成"] } },
      transforms: { f_order: { mode: "enum", enum_map: { "示例文本": "未知选项" } } },
    }), sources());
    expect(enumDraft.some((problem) => problem.message.includes("不在目标列选项中"))).toBe(true);
  });

  it("normalizes transform records and removes direct mode entries", () => {
    const draft = setTransform(tencentDraft(), "f_order", { mode: "direct" });
    expect(draft.transforms.f_order).toBeUndefined();
    const withDate = setTransform(draft, "f_order", { mode: "date" });
    expect(withDate.transforms.f_order).toEqual({ mode: "date", date_format: "YYYY-MM-DD" });
  });
});

describe("schema maintenance", () => {
  it("adds fields with unique ids", () => {
    const first = addSchemaField(baseSchema());
    expect(first.fieldId).toBe("field_3");
    const second = addSchemaField(first.schema);
    expect(second.fieldId).toBe("field_4");
    expect(second.schema[second.fieldId].title).toBe("新字段");
  });

  it("toggles required with the legacy * title prefix", () => {
    const schema = updateSchemaFieldRequired(baseSchema(), "f_order", true);
    expect(schema.f_order.title).toBe("*订单号");
    expect(updateSchemaFieldRequired(schema, "f_order", false).f_order.title).toBe("订单号");
    expect(updateSchemaField(baseSchema(), "f_order", { type: "number" }).f_order.type).toBe("number");
  });

  it("removes a field together with its mapping and transform", () => {
    const draft = tencentDraft({
      transforms: { f_order: { mode: "date", date_format: "YYYY-MM-DD" } },
    });
    const next = removeSchemaField(draft, "f_order");
    expect(next.schema.f_order).toBeUndefined();
    expect(next.field_mappings.map((mapping) => mapping.target_field_id)).toEqual(["f_desc"]);
    expect(next.transforms.f_order).toBeUndefined();
  });

  it("renames a field and moves mappings/transforms", () => {
    const draft = tencentDraft({
      transforms: { f_order: { mode: "date", date_format: "YYYY-MM-DD" } },
    });
    const next = renameSchemaField(draft, "f_order", "f_number");
    expect(next).not.toBeNull();
    expect(next!.schema.f_number.title).toBe("订单号");
    expect(next!.field_mappings[1].target_field_id).toBe("f_number");
    expect(next!.transforms.f_number.mode).toBe("date");
    expect(renameSchemaField(draft, "f_order", "f_desc")).toBeNull();
    expect(renameSchemaField(draft, "f_order", "  ")).toBeNull();
  });

  it("applies a JSON schema and drops orphaned mappings", () => {
    const draft = tencentDraft();
    const next = applySchema(draft, { only: { title: "唯一列", type: "text" } });
    expect(Object.keys(next.schema)).toEqual(["only"]);
    expect(next.field_mappings).toEqual([]);
    expect(() => parseSchemaJson("[]")).toThrow("顶层必须是 JSON 对象");
    expect(() => parseSchemaJson("{oops")).toThrow("不是有效 JSON");
    expect(parseSchemaJson('{" f ":{"title":" 列 ","type":" text ","enum":["a", 1]}}')).toEqual({
      f: { title: "列", type: "text", enum: ["a", "1"] },
    });
  });
});

describe("mapping helpers", () => {
  it("parses and formats default values by target type", () => {
    expect(parseMappingDefault("3", "number")).toBe(3);
    expect(parseMappingDefault("是", "checkbox")).toBe(true);
    expect(parseMappingDefault("待处理, 已完成", "multiple_select")).toEqual(["待处理", "已完成"]);
    expect(parseMappingDefault('["a","b"]', "multiple_select")).toEqual(["a", "b"]);
    expect(mappingDefaultText(["a", "b"])).toBe("a, b");
    expect(mappingDefaultText(undefined)).toBe("");
  });

  it("parses enum maps from line text", () => {
    expect(parseEnumMap("待处理=已完成\n 已关闭 = 已完成 \n\n")).toEqual({
      待处理: "已完成",
      已关闭: "已完成",
    });
    expect(enumMapText({ a: "b" })).toBe("a=b");
  });

  it("adds, updates and removes mappings", () => {
    const draft = addMapping(tencentDraft({
      schema: { ...baseSchema(), f_extra: { title: "备注", type: "text" } },
    }), mappingSourceOptions(baseConfig(), "strategy-issue"));
    expect(draft.field_mappings).toHaveLength(3);
    expect(draft.field_mappings[2].target_field_id).toBe("f_extra");

    const updated = updateMapping(draft, 2, { source_key: "order_no" });
    expect(updated.field_mappings[2].source_key).toBe("order_no");

    const removed = removeMapping(updated, 2);
    expect(removed.field_mappings).toHaveLength(2);
  });

  it("exposes the transform mode catalogue", () => {
    expect(TRANSFORM_MODES.map((mode) => mode.value)).toEqual([
      "direct",
      "constant",
      "template",
      "date",
      "enum",
    ]);
  });
});

describe("import and save", () => {
  it("imports a legacy template schema and mappings", () => {
    const draft = tencentDraft({
      transforms: { f_order: { mode: "date", date_format: "YYYY" }, f_desc: { mode: "constant", constant: "x" } },
    });
    const next = importLegacyTemplate(draft, legacyTemplate({
      id: "legacy-1",
      schema: { t_one: { title: "一", type: "text" }, t_two: { title: "二", type: "number" } },
      field_mappings: [
        { source_key: "order_no", target_field_id: "t_two", target_type: "number", required: false, default_value: "0" },
      ],
    }));
    expect(next.template_id).toBe("legacy-1");
    expect(Object.keys(next.schema)).toEqual(["t_one", "t_two"]);
    expect(next.field_mappings).toHaveLength(1);
    expect(next.field_mappings[0].target_type).toBe("number");
    expect(next.transforms).toEqual({});
  });

  it("writes schema, mappings and transforms back and syncs the legacy mirror", () => {
    const config = baseConfig();
    const target = tencentTarget({
      config: {
        template_id: "default",
        batch_size: 20,
        prompt_id: "strategy-issue",
        schema: baseSchema(),
        field_mappings: [
          { source_key: "problem_description", target_field_id: "f_desc", target_type: "text", required: true, default_value: "" },
          { source_key: "order_no", target_field_id: "f_order", target_type: "text", required: false, default_value: "0" },
        ],
        transforms: { f_order: { mode: "constant", constant: "待评估" } },
        auto_sync: true,
      },
    });
    const built = buildPublishSave(
      config,
      [{ id: "excel", name: "Excel", kind: "excel", config: {} }, target],
      { ...normalizeUpload(config.smart_sheet.upload), corpid: "corp-123" },
    );
    expect(built.targets).toHaveLength(2);
    const savedTarget = built.targets[1];
    expect(savedTarget.config.batch_size).toBe(20);
    expect(savedTarget.config.auto_sync).toBe(true);
    expect((savedTarget.config.transforms as Record<string, { mode: string }>).f_order.mode).toBe("constant");
    expect(Object.keys(savedTarget.config.schema as Record<string, unknown>)).toEqual(["f_desc", "f_order"]);

    const templates = built.config.smart_sheet.templates;
    expect(templates).toHaveLength(1);
    expect(templates[0].id).toBe("default");
    expect(Object.keys(templates[0].schema)).toEqual(["f_desc", "f_order"]);
    expect(templates[0].field_mappings).toHaveLength(2);
    expect("transforms" in templates[0]).toBe(false);
    expect(built.config.smart_sheet.default_template_id).toBe("default");
    expect(built.config.smart_sheet.upload.corpid).toBe("corp-123");
    expect(built.config.publish_targets).toBe(built.targets);
  });

  it("detects duplicate template ids across Tencent targets", () => {
    const config = baseConfig();
    const targets = [
      tencentTarget({ id: "tencent:1", config: { template_id: "same" } }),
      tencentTarget({ id: "tencent:2", config: { template_id: "same" } }),
      tencentTarget({ id: "tencent:3", config: { template_id: "other" } }),
      { id: "excel", name: "Excel", kind: "excel" as const, config: {} },
    ];
    expect(duplicateTemplateIds(config, targets)).toEqual(["same"]);
  });

  it("keeps the previous default template only while it still exists", () => {
    const config = baseConfig();
    const built = buildPublishSave(
      { ...config, smart_sheet: { ...config.smart_sheet, default_template_id: "gone" } },
      [tencentTarget({ config: { schema: baseSchema(), field_mappings: [] } })],
      normalizeUpload(config.smart_sheet.upload),
    );
    expect(built.config.smart_sheet.default_template_id).toBe("default");
  });

  it("drops the mirror when no Tencent target remains", () => {
    const config = baseConfig();
    const built = buildPublishSave(config, [{ id: "excel", name: "Excel", kind: "excel", config: {} }], normalizeUpload(config.smart_sheet.upload));
    expect(built.config.smart_sheet.templates).toEqual([]);
    expect(built.config.smart_sheet.default_template_id).toBe("");
  });
});

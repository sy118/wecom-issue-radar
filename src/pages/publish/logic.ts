import type {
  AppConfig,
  IssueFieldDefinition,
  PublishTarget,
  SmartSheetFieldMapping,
  SmartSheetFieldSchema,
} from "../../types";
import {
  isRequiredSchemaTitle,
  normalizeSmartSheetSchema,
  smartSheetSchemaProblems,
  stripRequiredSchemaTitle,
  withRequiredSchemaTitle,
} from "../../lib/smartSheetSchema";
import {
  FALLBACK_ISSUE_FIELDS,
  SYSTEM_MAPPING_SOURCES,
  type LegacyTemplate,
  type MappingProblem,
  type PreviewResult,
  type PreviewRow,
  type PromptOption,
  type PublishSaveResult,
  type PublishSchema,
  type PublishTransform,
  type PublishTransformMode,
  type PublishTransforms,
  type SourceOption,
  type TencentDraft,
  type UploadDraft,
} from "./types";

const TENCENT_TARGET_PREFIX = "tencent:";
const LEGACY_TENCENT_TARGET_PREFIX = "tencent_";
const DEFAULT_BATCH_SIZE = 50;

function stripTencentTargetPrefix(targetId: string): string {
  if (targetId.startsWith(TENCENT_TARGET_PREFIX)) {
    return targetId.slice(TENCENT_TARGET_PREFIX.length);
  }
  if (targetId.startsWith(LEGACY_TENCENT_TARGET_PREFIX)) {
    return targetId.slice(LEGACY_TENCENT_TARGET_PREFIX.length);
  }
  return targetId;
}

export const TRANSFORM_MODES: Array<{ value: PublishTransformMode; label: string; hint: string }> = [
  { value: "direct", label: "直接使用", hint: "来源值原样写入目标列" },
  { value: "constant", label: "固定值", hint: "忽略来源，始终写入同一个值" },
  { value: "template", label: "模板拼接", hint: "用 {{字段}} 组合多个来源" },
  { value: "date", label: "日期格式化", hint: "解析来源日期后按格式输出" },
  { value: "enum", label: "枚举映射", hint: "把来源值转换成目标列的选项" },
];

export const SYSTEM_SOURCE_LABELS: Record<string, string> = {
  $date: "导出日期",
  $images: "图片列表",
  $sender: "发送人",
  $message_time: "消息时间",
  $issue_key: "问题标识",
};

export const SYSTEM_SOURCE_TYPES: Record<string, string> = {
  $date: "date",
  $images: "multiple_select",
  $sender: "text",
  $message_time: "datetime",
  $issue_key: "text",
};

export const SMART_SHEET_TYPE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "text", label: "文本" },
  { value: "single_select", label: "单选" },
  { value: "multiple_select", label: "多选" },
  { value: "date_time", label: "日期时间" },
  { value: "image", label: "图片" },
  { value: "number", label: "数字" },
  { value: "checkbox", label: "复选框" },
  { value: "url", label: "链接" },
];

const isRecord = (value: unknown): value is Record<string, unknown> => Boolean(value)
  && typeof value === "object"
  && !Array.isArray(value);

const text = (value: unknown): string => (typeof value === "string" ? value.trim() : "");

export const clampBatchSize = (value: unknown): number => {
  const number = Number(value);
  if (!Number.isFinite(number)) return DEFAULT_BATCH_SIZE;
  return Math.min(50, Math.max(1, Math.round(number)));
};

export function mappingDefaultText(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function isEmptyDefault(value: unknown): boolean {
  return value === undefined
    || value === null
    || value === ""
    || (Array.isArray(value) && value.length === 0);
}

export function parseMappingDefault(value: string, targetType: string): unknown {
  if (!value.trim()) return "";
  if (targetType === "multiple_select") {
    try {
      const parsed: unknown = JSON.parse(value);
      if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean);
    } catch {
      // Comma/newline input is friendlier for manually entered defaults.
    }
    return value.split(/[,，\n]+/).map((item) => item.trim()).filter(Boolean);
  }
  if (targetType === "number") {
    const number = Number(value);
    return Number.isFinite(number) ? number : value;
  }
  if (targetType === "checkbox" || targetType === "boolean") {
    return ["true", "1", "是", "yes"].includes(value.trim().toLowerCase());
  }
  return value;
}

export function enumMapText(map: Record<string, string> | undefined): string {
  if (!map) return "";
  return Object.entries(map).map(([source, target]) => `${source}=${target}`).join("\n");
}

export function parseEnumMap(value: string): Record<string, string> {
  const map: Record<string, string> = {};
  for (const line of value.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const separator = trimmed.indexOf("=");
    if (separator <= 0) continue;
    const source = trimmed.slice(0, separator).trim();
    const target = trimmed.slice(separator + 1).trim();
    if (source) map[source] = target;
  }
  return map;
}

function normalizeFieldList(value: unknown): IssueFieldDefinition[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map((field) => ({
    key: text(field.key),
    label: text(field.label) || text(field.key),
    type: (text(field.type) || "text") as IssueFieldDefinition["type"],
    required: Boolean(field.required),
    instruction: text(field.instruction),
    options: Array.isArray(field.options) ? field.options.map(String) : [],
    default_value: field.default_value,
  })).filter((field) => field.key);
}

export function defaultIssueFields(config: AppConfig): IssueFieldDefinition[] {
  const configured = normalizeFieldList(config.prompts?.default_issue_fields);
  if (configured.length) return configured;
  for (const prompt of config.prompts?.items ?? []) {
    const fields = normalizeFieldList(prompt.issue_fields);
    if (fields.length) return fields;
  }
  return FALLBACK_ISSUE_FIELDS;
}

/** Issue-analysis prompts first (`ai_strategies` is canonical in config v4, `prompts` is the mirror). */
export function issueAnalysisPromptOptions(config: AppConfig): PromptOption[] {
  const strategies = (config.ai_strategies ?? []).filter(
    (strategy) => (strategy.scene ?? "issue_analysis") === "issue_analysis" && Boolean(strategy.id),
  );
  const prompts = (config.prompts?.items ?? []).filter(
    (prompt) => (prompt.scene ?? "issue_analysis") === "issue_analysis" && Boolean(prompt.id),
  );
  const seen = new Set<string>();
  const options: PromptOption[] = [];
  const strategyIds = new Set(strategies.map((strategy) => strategy.id));
  for (const item of [...strategies, ...prompts]) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    const fields = normalizeFieldList(item.issue_fields);
    options.push({
      id: item.id,
      name: text(item.name) || item.id,
      fields: fields.length ? fields : defaultIssueFields(config),
      strategy: strategyIds.has(item.id),
    });
  }
  if (!options.length) {
    return [{
      id: "",
      name: "默认问题字段",
      fields: defaultIssueFields(config),
      strategy: false,
    }];
  }
  return options;
}

export function resolvePromptId(config: AppConfig, requested: string | undefined): string {
  const options = issueAnalysisPromptOptions(config);
  const wanted = text(requested);
  if (options.some((option) => option.id === wanted)) return wanted;
  const fallback = text(config.prompts?.default_id);
  if (options.some((option) => option.id === fallback)) return fallback;
  return options[0]?.id ?? "";
}

export function mappingSourceOptions(config: AppConfig, promptId: string | undefined): SourceOption[] {
  const options = issueAnalysisPromptOptions(config);
  const prompt = options.find((option) => option.id === resolvePromptId(config, promptId)) ?? options[0];
  const sources: SourceOption[] = SYSTEM_MAPPING_SOURCES.map((key) => ({
    key,
    label: SYSTEM_SOURCE_LABELS[key] ?? key,
    type: SYSTEM_SOURCE_TYPES[key] ?? "text",
    system: true,
    options: [],
  }));
  const seen = new Set(sources.map((source) => source.key));
  for (const field of prompt?.fields ?? []) {
    const key = field.key.trim();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    sources.push({
      key,
      label: field.label || key,
      type: field.type,
      system: false,
      options: field.options,
    });
  }
  return sources;
}

export function sourceLabel(sources: SourceOption[], key: string): string {
  return sources.find((source) => source.key === key)?.label ?? key;
}

export function invalidSourceIndexes(
  mappings: SmartSheetFieldMapping[],
  sources: SourceOption[],
): number[] {
  const keys = new Set(sources.map((source) => source.key));
  return mappings
    .map((mapping, index) => (keys.has(mapping.source_key) ? -1 : index))
    .filter((index) => index >= 0);
}

function normalizeDraftMappings(value: unknown, schema: PublishSchema): SmartSheetFieldMapping[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map((raw) => {
    const targetFieldId = text(raw.target_field_id) || text(raw.target);
    const target = schema[targetFieldId];
    return {
      source_key: text(raw.source_key) || text(raw.source),
      target_field_id: targetFieldId,
      target_type: text(raw.target_type) || text(target?.type) || "text",
      required: Boolean(raw.required) || isRequiredSchemaTitle(target?.title),
      default_value: raw.default_value ?? raw.default ?? "",
    };
  });
}

function sanitizeTransform(value: unknown): PublishTransform | null {
  if (!isRecord(value)) return null;
  const mode = text(value.mode) as PublishTransformMode;
  if (!TRANSFORM_MODES.some((option) => option.value === mode)) return null;
  const transform: PublishTransform = { mode };
  if (mode === "constant") transform.constant = typeof value.constant === "string" ? value.constant : "";
  if (mode === "template") transform.template = typeof value.template === "string" ? value.template : "";
  if (mode === "date") {
    transform.date_format = typeof value.date_format === "string" && value.date_format.trim()
      ? value.date_format
      : "YYYY-MM-DD";
  }
  if (mode === "enum" && isRecord(value.enum_map)) {
    transform.enum_map = Object.fromEntries(
      Object.entries(value.enum_map).map(([key, item]) => [key, String(item)]),
    );
  }
  return transform;
}

export function normalizeTransforms(value: unknown): PublishTransforms {
  if (!isRecord(value)) return {};
  const transforms: PublishTransforms = {};
  for (const [targetFieldId, raw] of Object.entries(value)) {
    const transform = sanitizeTransform(raw);
    if (transform) transforms[targetFieldId] = transform;
  }
  return transforms;
}

function cleanTransforms(
  transforms: PublishTransforms,
  mappings: SmartSheetFieldMapping[],
): PublishTransforms {
  const targetIds = new Set(mappings.map((mapping) => mapping.target_field_id));
  const result: PublishTransforms = {};
  for (const [targetFieldId, transform] of Object.entries(transforms)) {
    if (!targetIds.has(targetFieldId) || transform.mode === "direct") continue;
    const sanitized = sanitizeTransform(transform);
    if (sanitized) result[targetFieldId] = sanitized;
  }
  return result;
}

/**
 * Materializes the editable target list. Configs produced by v4 already carry
 * `publish_targets`; older configs only have the legacy `smart_sheet.templates`
 * mirror, so those are expanded here without losing schema/mappings.
 */
export function targetsFromConfig(config: AppConfig): PublishTarget[] {
  if (config.publish_targets && config.publish_targets.length > 0) {
    return config.publish_targets.map((target) => ({
      ...target,
      config: isRecord(target.config) ? { ...target.config } : {},
    }));
  }
  const targets: PublishTarget[] = [
    { id: "excel", name: "Excel 导出", kind: "excel", config: {} },
    { id: "markdown", name: "Markdown 导出", kind: "markdown", config: {} },
  ];
  for (const template of config.smart_sheet?.templates ?? []) {
    targets.push({
      id: template.id,
      name: template.name || template.id,
      kind: "tencent_smartsheet",
      config: {
        template_id: template.id,
        url: template.url,
        webhook_url: template.webhook_url,
        webhook_url_env: template.webhook_url_env,
        batch_size: template.batch_size,
        schema: normalizeSmartSheetSchema(template.schema),
        field_mappings: Array.isArray(template.field_mappings) ? template.field_mappings : [],
      },
    });
  }
  return targets;
}

export function deriveTemplateId(config: AppConfig, target: PublishTarget): string {
  const raw = isRecord(target.config) ? target.config : {};
  const configured = text(raw.template_id);
  if (configured) return configured;
  const derived = stripTencentTargetPrefix(target.id);
  if (derived) return derived;
  return text(config.smart_sheet?.default_template_id) || "default";
}

export function readTencentDraft(config: AppConfig, target: PublishTarget): TencentDraft {
  const raw = isRecord(target.config) ? target.config : {};
  const schema = normalizeSmartSheetSchema(raw.schema);
  return {
    template_id: deriveTemplateId(config, target),
    url: text(raw.url),
    webhook_url: text(raw.webhook_url),
    webhook_url_env: text(raw.webhook_url_env),
    batch_size: clampBatchSize(raw.batch_size),
    auto_sync: raw.auto_sync === true,
    prompt_id: resolvePromptId(config, typeof raw.prompt_id === "string" ? raw.prompt_id : ""),
    schema,
    field_mappings: normalizeDraftMappings(raw.field_mappings, schema),
    transforms: normalizeTransforms(raw.transforms),
  };
}

export function targetConfigFromDraft(
  draft: TencentDraft,
  existing: Record<string, unknown>,
  config: AppConfig,
  targetId: string,
): Record<string, unknown> {
  const schema = normalizeSmartSheetSchema(draft.schema);
  const mappings = normalizeDraftMappings(draft.field_mappings, schema).map((mapping) => {
    const target = schema[mapping.target_field_id];
    return {
      ...mapping,
      target_type: text(target?.type) || mapping.target_type || "text",
      required: mapping.required || isRequiredSchemaTitle(target?.title),
    };
  });
  return {
    ...existing,
    template_id: text(draft.template_id)
      || stripTencentTargetPrefix(targetId)
      || text(config.smart_sheet?.default_template_id)
      || "default",
    url: text(draft.url),
    webhook_url: text(draft.webhook_url),
    webhook_url_env: text(draft.webhook_url_env),
    batch_size: clampBatchSize(draft.batch_size),
    auto_sync: Boolean(draft.auto_sync),
    prompt_id: text(draft.prompt_id),
    schema,
    field_mappings: mappings,
    transforms: cleanTransforms(draft.transforms, mappings),
  };
}

function targetFieldTitle(schema: PublishSchema, fieldId: string): string {
  const title = schema[fieldId]?.title;
  return stripRequiredSchemaTitle(typeof title === "string" ? title : "") || fieldId;
}

function targetEnumOptions(schema: PublishSchema, fieldId: string): string[] {
  const options = schema[fieldId]?.enum;
  return Array.isArray(options) ? options.map(String) : [];
}

function mappingRequired(mapping: SmartSheetFieldMapping, schema: PublishSchema): boolean {
  return mapping.required || isRequiredSchemaTitle(schema[mapping.target_field_id]?.title);
}

const PLACEHOLDER_PATTERN = /\{\{\s*([^{}]+?)\s*\}\}/g;

export function templatePlaceholders(template: string): string[] {
  return [...template.matchAll(PLACEHOLDER_PATTERN)].map((match) => match[1].trim()).filter(Boolean);
}

export function displayValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (Array.isArray(value)) return value.map(displayValue).filter(Boolean).join("，");
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function toDate(value: unknown): Date | null {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  if (typeof value !== "string" || !value.trim()) return null;
  const raw = value.trim();
  const candidates = [
    raw,
    raw.replace(" ", "T"),
    /^\d{4}-\d{2}-\d{2}$/.test(raw) ? `${raw}T00:00:00` : "",
  ].filter(Boolean);
  for (const candidate of candidates) {
    const date = new Date(candidate);
    if (!Number.isNaN(date.getTime())) return date;
  }
  return null;
}

const pad = (value: number): string => String(value).padStart(2, "0");

export function formatDateValue(value: unknown, format: string): string {
  const date = toDate(value);
  if (!date) return displayValue(value);
  const pattern = format.trim() || "YYYY-MM-DD";
  return pattern.replace(/YYYY|YY|MM|DD|HH|mm|ss/g, (token) => {
    switch (token) {
      case "YYYY": return String(date.getFullYear());
      case "YY": return String(date.getFullYear()).slice(-2);
      case "MM": return pad(date.getMonth() + 1);
      case "DD": return pad(date.getDate());
      case "HH": return pad(date.getHours());
      case "mm": return pad(date.getMinutes());
      case "ss": return pad(date.getSeconds());
      default: return token;
    }
  });
}

export function isValidDateFormat(format: string): boolean {
  return /YYYY|YY|MM|DD|HH|mm|ss/.test(format);
}

export function renderTemplate(template: string, context: Record<string, unknown>): string {
  return template.replace(PLACEHOLDER_PATTERN, (_match, rawKey: string) => {
    const key = rawKey.trim();
    return displayValue(context[key]);
  });
}

function resolveMappedValue(
  mapping: SmartSheetFieldMapping,
  transform: PublishTransform,
  schema: PublishSchema,
  context: Record<string, unknown>,
): unknown {
  const raw = context[mapping.source_key];
  const source = isEmptyDefault(raw) ? mapping.default_value : raw;
  switch (transform.mode) {
    case "constant":
      return transform.constant ?? "";
    case "template":
      return renderTemplate(transform.template ?? "", {
        ...context,
        [mapping.source_key]: source,
        $value: source,
      });
    case "date": {
      const dateSource = isEmptyDefault(source) ? context.$date : source;
      return formatDateValue(dateSource, transform.date_format ?? "YYYY-MM-DD");
    }
    case "enum": {
      const key = displayValue(source);
      const mapped = transform.enum_map?.[key];
      if (mapped !== undefined) return mapped;
      const options = targetEnumOptions(schema, mapping.target_field_id);
      if (options.includes(key)) return key;
      return "";
    }
    case "direct":
    default:
      return source;
  }
}

export function sampleBySourceType(type: string, options: string[] = []): unknown {
  switch (type) {
    case "single_select": return options[0] ?? "选项A";
    case "multiple_select": return options.length ? options.slice(0, 2) : ["选项A", "选项B"];
    case "boolean": return true;
    case "number": return 42;
    case "date": return "2026-10-22";
    case "datetime": return "2026-10-22 09:30";
    case "url": return "https://example.com/issues/1";
    case "long_text": return "示例问题描述：导出的订单缺少物流单号，需要人工核对。";
    case "image": return ["https://example.com/demo-1.png"];
    default: return "示例文本";
  }
}

export function buildSampleIssue(
  sources: SourceOption[],
  now: Date = new Date(),
): Record<string, unknown> {
  const date = formatDateValue(now, "YYYY-MM-DD");
  const dateTime = formatDateValue(now, "YYYY-MM-DD HH:mm");
  const sample: Record<string, unknown> = {
    $date: date,
    $images: ["https://example.com/demo-1.png"],
    $sender: "张三",
    $message_time: dateTime,
    $issue_key: "issue-demo-001",
  };
  for (const source of sources) {
    if (source.system) continue;
    sample[source.key] = sampleBySourceType(source.type, source.options ?? []);
  }
  return sample;
}

function transformProblems(
  draft: TencentDraft,
  sources: SourceOption[],
): MappingProblem[] {
  const problems: MappingProblem[] = [];
  const sourceKeys = new Set(sources.map((source) => source.key));
  draft.field_mappings.forEach((mapping, index) => {
    const transform = draft.transforms[mapping.target_field_id];
    if (!transform || transform.mode === "direct") return;
    const label = targetFieldTitle(draft.schema, mapping.target_field_id);
    if (transform.mode === "constant" && !(transform.constant ?? "").trim()) {
      problems.push({
        key: `transform:${index}:constant`,
        level: "error",
        mappingIndex: index,
        message: `“${label}”使用固定值转换，但固定值不能为空`,
      });
    }
    if (transform.mode === "template") {
      const template = transform.template ?? "";
      if (!template.trim()) {
        problems.push({
          key: `transform:${index}:template`,
          level: "error",
          mappingIndex: index,
          message: `“${label}”使用模板转换，但模板不能为空`,
        });
      } else {
        const unknown = templatePlaceholders(template).filter(
          (key) => key !== "$value" && !sourceKeys.has(key),
        );
        if (!templatePlaceholders(template).length) {
          problems.push({
            key: `transform:${index}:template-empty`,
            level: "warning",
            mappingIndex: index,
            message: `“${label}”的模板没有任何 {{占位符}}，将输出固定文本`,
          });
        }
        for (const key of unknown) {
          problems.push({
            key: `transform:${index}:template:${key}`,
            level: "warning",
            mappingIndex: index,
            message: `“${label}”的模板引用了不存在的字段 {{${key}}}`,
          });
        }
      }
    }
    if (transform.mode === "date" && !isValidDateFormat(transform.date_format ?? "")) {
      problems.push({
        key: `transform:${index}:date`,
        level: "warning",
        mappingIndex: index,
        message: `“${label}”的日期格式不含 YYYY/MM/DD 等占位符，将原样输出`,
      });
    }
    if (transform.mode === "enum") {
      const options = targetEnumOptions(draft.schema, mapping.target_field_id);
      const map = transform.enum_map ?? {};
      if (!Object.keys(map).length) {
        problems.push({
          key: `transform:${index}:enum-empty`,
          level: "warning",
          mappingIndex: index,
          message: `“${label}”使用枚举映射，但尚未配置“源值=目标值”`,
        });
      }
      for (const [sourceValue, targetValue] of Object.entries(map)) {
        if (options.length && !options.includes(targetValue)) {
          problems.push({
            key: `transform:${index}:enum:${sourceValue}`,
            level: "warning",
            mappingIndex: index,
            message: `“${label}”的枚举映射结果“${targetValue}”不在目标列选项中`,
          });
        }
      }
    }
  });
  return problems;
}

export function validateDraft(draft: TencentDraft, sources: SourceOption[]): MappingProblem[] {
  const problems: MappingProblem[] = smartSheetSchemaProblems(draft.schema).map((problem) => ({
    key: `schema:${problem.fieldId}:${problem.message}`,
    level: problem.level,
    message: problem.message,
  }));
  const sourceKeys = new Set(sources.map((source) => source.key));
  const usedTargets = new Map<string, number>();
  draft.field_mappings.forEach((mapping, index) => {
    const targetFieldId = mapping.target_field_id.trim();
    const target = draft.schema[targetFieldId];
    if (!targetFieldId) {
      problems.push({
        key: `mapping:${index}:target-empty`,
        level: "error",
        mappingIndex: index,
        message: `第 ${index + 1} 条映射未选择目标字段`,
      });
      return;
    }
    if (!target) {
      problems.push({
        key: `mapping:${index}:target-missing`,
        level: "error",
        mappingIndex: index,
        message: `目标字段不存在：${targetFieldId}`,
      });
    } else if (usedTargets.has(targetFieldId)) {
      problems.push({
        key: `mapping:${index}:duplicate`,
        level: "error",
        mappingIndex: index,
        message: `目标字段重复映射：${targetFieldTitle(draft.schema, targetFieldId)}`,
      });
    } else {
      usedTargets.set(targetFieldId, index);
    }
    if (target && text(target.type) && mapping.target_type !== target.type) {
      problems.push({
        key: `mapping:${index}:type`,
        level: "error",
        mappingIndex: index,
        message: `“${targetFieldTitle(draft.schema, targetFieldId)}”的映射类型应为 ${target.type}`,
      });
    }
    if (!mapping.source_key.trim()) {
      problems.push({
        key: `mapping:${index}:source-empty`,
        level: "error",
        mappingIndex: index,
        message: `第 ${index + 1} 条映射未选择来源字段`,
      });
    } else if (!sourceKeys.has(mapping.source_key)) {
      problems.push({
        key: `mapping:${index}:source-invalid`,
        level: "warning",
        mappingIndex: index,
        message: `来源字段“${mapping.source_key}”不在当前提示词中；切换回原提示词可恢复`,
      });
    }
    if (mappingRequired(mapping, draft.schema)
      && (!mapping.source_key.trim() || !sourceKeys.has(mapping.source_key))
      && isEmptyDefault(mapping.default_value)) {
      problems.push({
        key: `mapping:${index}:required`,
        level: "error",
        mappingIndex: index,
        message: `必填字段“${targetFieldTitle(draft.schema, targetFieldId)}”缺少有效来源或默认值`,
      });
    }
  });

  if (draft.field_mappings.length && Object.keys(draft.schema).length) {
    for (const fieldId of Object.keys(draft.schema)) {
      if (!usedTargets.has(fieldId) && isRequiredSchemaTitle(draft.schema[fieldId]?.title)) {
        problems.push({
          key: `schema:mapping-missing:${fieldId}`,
          level: "error",
          message: `缺少必填目标字段映射：${targetFieldTitle(draft.schema, fieldId)}`,
        });
      }
    }
  }

  if (Object.keys(draft.schema).length && !draft.field_mappings.length) {
    problems.push({
      key: "mapping:empty",
      level: "error",
      message: "尚未配置字段映射",
    });
  }

  problems.push(...transformProblems(draft, sources));
  return problems.filter((problem, index) => problems.findIndex(
    (candidate) => candidate.key === problem.key,
  ) === index);
}

export function previewDraft(
  draft: TencentDraft,
  sources: SourceOption[],
  now: Date = new Date(),
): PreviewResult {
  const sample = buildSampleIssue(sources, now);
  const sourceKeys = new Set(sources.map((source) => source.key));
  const mappingsByTarget = new Map<string, SmartSheetFieldMapping>();
  for (const mapping of draft.field_mappings) {
    if (!mappingsByTarget.has(mapping.target_field_id)) {
      mappingsByTarget.set(mapping.target_field_id, mapping);
    }
  }
  const rows: PreviewRow[] = Object.entries(draft.schema).map(([fieldId, field]) => {
    const mapping = mappingsByTarget.get(fieldId);
    const transform = draft.transforms[fieldId] ?? { mode: "direct" as const };
    const value = mapping ? resolveMappedValue(mapping, transform, draft.schema, sample) : "";
    return {
      fieldId,
      title: targetFieldTitle(draft.schema, fieldId),
      type: text(field.type) || "text",
      sourceKey: mapping?.source_key ?? "",
      mode: transform.mode,
      value,
      display: displayValue(value),
      mapped: Boolean(mapping),
      sourceMissing: Boolean(mapping && !sourceKeys.has(mapping.source_key)),
    };
  });
  return {
    rows,
    problems: validateDraft(draft, sources),
    sample: sources.map((source) => ({
      key: source.key,
      label: source.label,
      system: source.system,
      display: displayValue(sample[source.key]),
    })),
  };
}

export function normalizeUpload(value: unknown): UploadDraft {
  const raw = isRecord(value) ? value : {};
  const number = (item: unknown, fallback: number) => {
    const parsed = Number(item);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
  };
  return {
    token_endpoint: text(raw.token_endpoint),
    image_upload_endpoint: text(raw.image_upload_endpoint),
    image_form_field: text(raw.image_form_field) || "media",
    propagation_wait_ms_after_uploads: number(raw.propagation_wait_ms_after_uploads, 1500),
    delay_ms_between_image_uploads: number(raw.delay_ms_between_image_uploads, 120),
    corpid: text(raw.corpid),
    corpsecret: text(raw.corpsecret),
  };
}

function templateFromDraft(target: PublishTarget, draft: TencentDraft): LegacyTemplate {
  const schema = normalizeSmartSheetSchema(draft.schema);
  return {
    id: text(draft.template_id) || target.id || "default",
    name: target.name || target.id,
    url: draft.url,
    webhook_url_env: draft.webhook_url_env,
    webhook_url: draft.webhook_url,
    batch_size: clampBatchSize(draft.batch_size),
    schema,
    field_mappings: normalizeDraftMappings(draft.field_mappings, schema).map((mapping) => ({
      ...mapping,
      target_type: text(schema[mapping.target_field_id]?.type) || mapping.target_type || "text",
      required: mapping.required || isRequiredSchemaTitle(schema[mapping.target_field_id]?.title),
    })),
  };
}

export function buildPublishSave(
  config: AppConfig,
  targets: PublishTarget[],
  upload: UploadDraft,
): PublishSaveResult {
  const drafts = new Map<string, TencentDraft>();
  for (const target of targets) {
    if (target.kind === "tencent_smartsheet") {
      drafts.set(target.id, readTencentDraft(config, target));
    }
  }
  const nextTargets: PublishTarget[] = targets.map((target) => {
    const draft = drafts.get(target.id);
    if (!draft) return target;
    return {
      ...target,
      config: targetConfigFromDraft(draft, target.config, config, target.id),
    };
  });
  const templates: LegacyTemplate[] = nextTargets
    .filter((target) => target.kind === "tencent_smartsheet")
    .map((target) => templateFromDraft(target, drafts.get(target.id)!));
  const templateIds = new Set(templates.map((template) => template.id));
  const requestedDefault = text(config.smart_sheet?.default_template_id);
  const nextSmartSheet = {
    ...config.smart_sheet,
    upload: { ...config.smart_sheet?.upload, ...upload },
    templates,
    default_template_id: templateIds.has(requestedDefault)
      ? requestedDefault
      : templates[0]?.id ?? "",
  };
  return {
    config: {
      ...config,
      publish_targets: nextTargets,
      smart_sheet: nextSmartSheet,
    },
    targets: nextTargets,
  };
}

export function duplicateTemplateIds(config: AppConfig, targets: PublishTarget[]): string[] {
  const seen = new Set<string>();
  const duplicates = new Set<string>();
  for (const target of targets) {
    if (target.kind !== "tencent_smartsheet") continue;
    const templateId = readTencentDraft(config, target).template_id.trim();
    if (!templateId) continue;
    if (seen.has(templateId)) duplicates.add(templateId);
    seen.add(templateId);
  }
  return [...duplicates];
}

export function importLegacyTemplate(draft: TencentDraft, template: LegacyTemplate): TencentDraft {
  const schema = normalizeSmartSheetSchema(template.schema);
  const mappings = normalizeDraftMappings(template.field_mappings, schema).map((mapping) => {
    const target = schema[mapping.target_field_id];
    return {
      ...mapping,
      target_type: text(target?.type) || mapping.target_type || "text",
      required: mapping.required || isRequiredSchemaTitle(target?.title),
    };
  });
  const transforms = Object.fromEntries(
    Object.entries(draft.transforms).filter(([fieldId]) => Boolean(schema[fieldId])),
  ) as PublishTransforms;
  return {
    ...draft,
    template_id: text(template.id) || draft.template_id,
    schema,
    field_mappings: mappings,
    transforms,
  };
}

export function nextSchemaFieldId(schema: PublishSchema): string {
  let index = Object.keys(schema).length + 1;
  let fieldId = `field_${index}`;
  while (schema[fieldId]) {
    index += 1;
    fieldId = `field_${index}`;
  }
  return fieldId;
}

export function addSchemaField(
  schema: PublishSchema,
  type = "text",
): { schema: PublishSchema; fieldId: string } {
  const fieldId = nextSchemaFieldId(schema);
  return {
    schema: {
      ...schema,
      [fieldId]: { title: "新字段", type, enum: [] },
    },
    fieldId,
  };
}

export function updateSchemaField(
  schema: PublishSchema,
  fieldId: string,
  patch: Partial<SmartSheetFieldSchema>,
): PublishSchema {
  const current = schema[fieldId] ?? { title: "", type: "text" };
  return {
    ...schema,
    [fieldId]: { ...current, ...patch },
  };
}

export function updateSchemaFieldRequired(
  schema: PublishSchema,
  fieldId: string,
  required: boolean,
): PublishSchema {
  const title = typeof schema[fieldId]?.title === "string" ? schema[fieldId].title : "";
  return updateSchemaField(schema, fieldId, { title: withRequiredSchemaTitle(title, required) });
}

export function removeSchemaField(
  draft: TencentDraft,
  fieldId: string,
): TencentDraft {
  const schema = { ...draft.schema };
  delete schema[fieldId];
  const transforms = { ...draft.transforms };
  delete transforms[fieldId];
  return {
    ...draft,
    schema,
    field_mappings: draft.field_mappings.filter((mapping) => mapping.target_field_id !== fieldId),
    transforms,
  };
}

export function renameSchemaField(
  draft: TencentDraft,
  from: string,
  to: string,
): TencentDraft | null {
  const nextId = to.trim();
  if (!nextId || nextId === from || draft.schema[nextId]) return null;
  const schema: PublishSchema = {};
  for (const [fieldId, field] of Object.entries(draft.schema)) {
    schema[fieldId === from ? nextId : fieldId] = field;
  }
  const transforms = { ...draft.transforms };
  if (transforms[from]) {
    transforms[nextId] = transforms[from];
    delete transforms[from];
  }
  return {
    ...draft,
    schema,
    transforms,
    field_mappings: draft.field_mappings.map((mapping) => (
      mapping.target_field_id === from ? { ...mapping, target_field_id: nextId } : mapping
    )),
  };
}

export function parseSchemaJson(value: string): PublishSchema {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error("Schema 不是有效 JSON");
  }
  if (!isRecord(parsed)) throw new Error("Schema 顶层必须是 JSON 对象");
  return normalizeSmartSheetSchema(parsed);
}

export function applySchema(
  draft: TencentDraft,
  schema: PublishSchema,
): TencentDraft {
  const mappings = draft.field_mappings
    .filter((mapping) => Boolean(schema[mapping.target_field_id]))
    .map((mapping) => {
      const target = schema[mapping.target_field_id];
      return {
        ...mapping,
        target_type: text(target?.type) || mapping.target_type || "text",
        required: mapping.required || isRequiredSchemaTitle(target?.title),
      };
    });
  const transforms = Object.fromEntries(
    Object.entries(draft.transforms).filter(([fieldId]) => Boolean(schema[fieldId])),
  ) as PublishTransforms;
  return { ...draft, schema, field_mappings: mappings, transforms };
}

export function addMapping(draft: TencentDraft, sources: SourceOption[]): TencentDraft {
  const usedTargets = new Set(draft.field_mappings.map((mapping) => mapping.target_field_id));
  const targetFieldId = Object.keys(draft.schema).find((fieldId) => !usedTargets.has(fieldId)) ?? "";
  if (!targetFieldId) return draft;
  const usedSources = new Set(draft.field_mappings.map((mapping) => mapping.source_key));
  const source = sources.find((option) => !usedSources.has(option.key)) ?? sources[0];
  const target = draft.schema[targetFieldId];
  return {
    ...draft,
    field_mappings: [
      ...draft.field_mappings,
      {
        source_key: source?.key ?? "",
        target_field_id: targetFieldId,
        target_type: text(target?.type) || "text",
        required: isRequiredSchemaTitle(target?.title),
        default_value: "",
      },
    ],
  };
}

export function updateMapping(
  draft: TencentDraft,
  index: number,
  patch: Partial<SmartSheetFieldMapping>,
): TencentDraft {
  return {
    ...draft,
    field_mappings: draft.field_mappings.map((mapping, mappingIndex) => (
      mappingIndex === index ? { ...mapping, ...patch } : mapping
    )),
  };
}

export function removeMapping(draft: TencentDraft, index: number): TencentDraft {
  const mapping = draft.field_mappings[index];
  if (!mapping) return draft;
  const transforms = { ...draft.transforms };
  delete transforms[mapping.target_field_id];
  return {
    ...draft,
    field_mappings: draft.field_mappings.filter((_, mappingIndex) => mappingIndex !== index),
    transforms,
  };
}

export function setTransform(
  draft: TencentDraft,
  targetFieldId: string,
  transform: PublishTransform,
): TencentDraft {
  const sanitized = sanitizeTransform(transform);
  if (!sanitized) return draft;
  const transforms = { ...draft.transforms };
  if (sanitized.mode === "direct") delete transforms[targetFieldId];
  else transforms[targetFieldId] = sanitized;
  return { ...draft, transforms };
}

export function problemsForTarget(
  config: AppConfig,
  target: PublishTarget,
): MappingProblem[] {
  if (target.kind !== "tencent_smartsheet") return [];
  const draft = readTencentDraft(config, target);
  return validateDraft(draft, mappingSourceOptions(config, draft.prompt_id));
}

export function errorCount(problems: MappingProblem[]): number {
  return problems.filter((problem) => problem.level === "error").length;
}

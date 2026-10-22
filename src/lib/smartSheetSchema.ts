import type { SmartSheetFieldSchema } from "../types";

export const SUPPORTED_SMART_SHEET_TARGET_TYPES = [
  "text",
  "single_select",
  "multiple_select",
  "date_time",
  "image",
  "number",
  "checkbox",
  "url",
] as const;

const supportedTargetTypes = new Set<string>(SUPPORTED_SMART_SHEET_TARGET_TYPES);

const isObject = (value: unknown): value is Record<string, unknown> => Boolean(value)
  && typeof value === "object"
  && !Array.isArray(value);

export interface SmartSheetExampleConversion {
  schema: Record<string, SmartSheetFieldSchema>;
  unsupportedTypes: string[];
}

export const SMART_SHEET_FIELD_TYPE_LABELS: Record<string, string> = {
  text: "文本",
  single_select: "单选",
  multiple_select: "多选",
  date_time: "日期时间",
  image: "图片",
  number: "数字",
  checkbox: "复选框",
  url: "链接",
};

export function isSupportedSmartSheetTargetType(type: string): boolean {
  return supportedTargetTypes.has(type.trim());
}

/** Coerces an arbitrary persisted `schema` block into the shape the editor expects. */
export function normalizeSmartSheetSchema(value: unknown): Record<string, SmartSheetFieldSchema> {
  if (!isObject(value)) return {};
  const schema: Record<string, SmartSheetFieldSchema> = {};
  for (const [rawFieldId, rawDefinition] of Object.entries(value)) {
    const fieldId = rawFieldId.trim();
    if (!fieldId) continue;
    const definition = isObject(rawDefinition) ? rawDefinition : {};
    const title = typeof definition.title === "string" ? definition.title.trim() : "";
    const type = typeof definition.type === "string" ? definition.type.trim() : "";
    const normalized: SmartSheetFieldSchema = { ...definition, title, type };
    if (definition.enum !== undefined) {
      normalized.enum = Array.isArray(definition.enum)
        ? definition.enum.map((item) => String(item).trim()).filter(Boolean)
        : [];
    }
    schema[fieldId] = normalized;
  }
  return schema;
}

/** Reports schema entries that cannot be published as-is. Warnings are prefixed separately. */
export function smartSheetSchemaProblems(
  schema: Record<string, SmartSheetFieldSchema>,
): Array<{ fieldId: string; level: "error" | "warning"; message: string }> {
  const problems: Array<{ fieldId: string; level: "error" | "warning"; message: string }> = [];
  const entries = Object.entries(schema);
  if (!entries.length) {
    problems.push({ fieldId: "", level: "error", message: "尚未定义目标字段 Schema" });
    return problems;
  }
  for (const [fieldId, field] of entries) {
    const title = typeof field.title === "string" ? field.title.trim() : "";
    const type = typeof field.type === "string" ? field.type.trim() : "";
    if (!title) {
      problems.push({ fieldId, level: "error", message: `目标字段 ${fieldId} 缺少名称` });
    }
    if (!type) {
      problems.push({ fieldId, level: "error", message: `目标字段 ${fieldId} 缺少类型` });
    } else if (!supportedTargetTypes.has(type)) {
      problems.push({
        fieldId,
        level: "warning",
        message: `目标字段 ${fieldId} 的类型 ${type} 暂不受智能表格预览支持`,
      });
    } else if ((type === "single_select" || type === "multiple_select")
      && !(Array.isArray(field.enum) && field.enum.length > 0)) {
      problems.push({
        fieldId,
        level: "warning",
        message: `目标字段 ${fieldId} 是选择类型，建议补充枚举选项`,
      });
    }
  }
  return problems;
}

/** Old configs mark a required Smart Sheet column with a leading `*` in its title. */
export function isRequiredSchemaTitle(title: unknown): boolean {
  return typeof title === "string" && title.trim().startsWith("*");
}

export function stripRequiredSchemaTitle(title: string): string {
  return title.replace(/^\*+\s*/, "").trim();
}

export function withRequiredSchemaTitle(title: string, required: boolean): string {
  const base = stripRequiredSchemaTitle(title) || "未命名字段";
  return required ? `*${base}` : base;
}

export function convertSmartSheetExampleData(text: string): SmartSheetExampleConversion {
  if (!text.trim()) throw new Error("请先粘贴腾讯文档生成的示例数据");

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("示例数据不是有效 JSON");
  }
  if (!isObject(parsed)) throw new Error("示例数据顶层必须是 JSON 对象");

  const rawSchema = Object.prototype.hasOwnProperty.call(parsed, "schema")
    ? parsed.schema
    : parsed;
  if (!isObject(rawSchema)) throw new Error("示例数据中的 schema 必须是 JSON 对象");

  const entries = Object.entries(rawSchema);
  if (!entries.length) throw new Error("示例数据中的 schema 不能为空");

  const schema: Record<string, SmartSheetFieldSchema> = {};
  const unsupportedTypes = new Set<string>();
  for (const [rawFieldId, rawDefinition] of entries) {
    const fieldId = rawFieldId.trim();
    if (!fieldId) throw new Error("示例数据包含空的字段 ID");
    if (!isObject(rawDefinition)) throw new Error(`字段 ${fieldId} 的定义必须是 JSON 对象`);

    const title = typeof rawDefinition.title === "string" ? rawDefinition.title.trim() : "";
    const type = typeof rawDefinition.type === "string" ? rawDefinition.type.trim() : "";
    if (!title) throw new Error(`字段 ${fieldId} 缺少名称 title`);
    if (!type) throw new Error(`字段 ${fieldId} 缺少类型 type`);

    const definition: SmartSheetFieldSchema = { ...rawDefinition, title, type };
    if (rawDefinition.enum !== undefined) {
      if (!Array.isArray(rawDefinition.enum) || rawDefinition.enum.some((value) => typeof value !== "string")) {
        throw new Error(`字段 ${fieldId} 的 enum 必须是字符串数组`);
      }
      definition.enum = rawDefinition.enum.map((value) => value.trim()).filter(Boolean);
    }
    schema[fieldId] = definition;
    if (!supportedTargetTypes.has(type)) unsupportedTypes.add(type);
  }

  return { schema, unsupportedTypes: [...unsupportedTypes].sort() };
}

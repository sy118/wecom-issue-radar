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

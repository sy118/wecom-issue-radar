import type { SmartSheetFieldSchema } from "../../types";
import {
  isRequiredSchemaTitle,
  SMART_SHEET_FIELD_TYPE_LABELS,
  stripRequiredSchemaTitle,
  withRequiredSchemaTitle,
} from "../../lib/smartSheetSchema";

/**
 * Tencent Docs Smart Sheet schemas copied from the web/API come in several
 * shapes: an internal `{field_id: {...}}` map, a fields array, or wrappers such
 * as `{fields:[...]}`, `{schema:[...]}`, `{columns:[...]}` and `{data:[...]}`.
 * This module detects the shape and normalizes it into the internal schema the
 * publish-target editor uses. It never touches the network or the draft.
 */

export type SemanticFieldType =
  | "text"
  | "long_text"
  | "number"
  | "single_select"
  | "multiple_select"
  | "boolean"
  | "date"
  | "image"
  | "url"
  | "unknown";

export interface TypeMapping {
  semantic: SemanticFieldType;
  /** Type stored in the internal Smart Sheet schema. */
  internal: string;
  /** Optional human readable downgrade note (e.g. image -> text). */
  note?: string;
}

export interface SchemaImportProblem {
  level: "error" | "warning";
  fieldId?: string;
  message: string;
}

export interface ImportedField {
  fieldId: string;
  title: string;
  type: string;
  originalType: string;
  options: string[];
  required: boolean;
}

export interface SchemaImportResult {
  schema: Record<string, SmartSheetFieldSchema>;
  fields: ImportedField[];
  problems: SchemaImportProblem[];
  notes: string[];
  typeCounts: Record<string, number>;
}

const TYPE_ALIASES: Record<string, SemanticFieldType> = {
  text: "text",
  string: "text",
  str: "text",
  单行文本: "text",
  单行: "text",
  文本: "text",
  短文本: "text",
  long_text: "long_text",
  longtext: "long_text",
  long: "long_text",
  multiline: "long_text",
  multi_line: "long_text",
  multi_line_text: "long_text",
  多行文本: "long_text",
  长文本: "long_text",
  段落: "long_text",
  number: "number",
  numeric: "number",
  int: "number",
  integer: "number",
  float: "number",
  decimal: "number",
  数字: "number",
  数值: "number",
  金额: "number",
  货币: "number",
  进度: "number",
  single_select: "single_select",
  singleselect: "single_select",
  single: "single_select",
  select: "single_select",
  option: "single_select",
  单选: "single_select",
  单选项: "single_select",
  下拉: "single_select",
  multiple_select: "multiple_select",
  multipleselect: "multiple_select",
  multi_select: "multiple_select",
  multiselect: "multiple_select",
  multi: "multiple_select",
  多选: "multiple_select",
  多选项: "multiple_select",
  checkbox: "boolean",
  check_box: "boolean",
  boolean: "boolean",
  bool: "boolean",
  是: "boolean",
  否: "boolean",
  是否: "boolean",
  "是/否": "boolean",
  复选框: "boolean",
  勾选: "boolean",
  date: "date",
  date_time: "date",
  datetime: "date",
  date_time_value: "date",
  日期: "date",
  日期时间: "date",
  时间: "date",
  created_time: "date",
  modified_time: "date",
  创建时间: "date",
  修改时间: "date",
  image: "image",
  images: "image",
  attachment: "image",
  attachments: "image",
  file: "image",
  picture: "image",
  图片: "image",
  附件: "image",
  文件: "image",
  url: "url",
  link: "url",
  hyperlink: "url",
  链接: "url",
  网址: "url",
  user: "unknown",
  users: "unknown",
  member: "unknown",
  person: "unknown",
  reference: "unknown",
  formula: "unknown",
  lookup: "unknown",
  人员: "unknown",
  成员: "unknown",
  创建人: "unknown",
  修改人: "unknown",
  引用: "unknown",
  公式: "unknown",
  phone: "unknown",
  email: "unknown",
  location: "unknown",
  barcode: "unknown",
  auto_number: "unknown",
  电话: "unknown",
  邮箱: "unknown",
  地区: "unknown",
  条码: "unknown",
  自动编号: "unknown",
};

const INTERNAL_BY_SEMANTIC: Record<SemanticFieldType, string> = {
  text: "text",
  // Tencent has no separate long-text column type; the write path treats it as text.
  long_text: "text",
  number: "number",
  single_select: "single_select",
  multiple_select: "multiple_select",
  // The publish pipeline accepts `checkbox`; keep the historical boolean semantics.
  boolean: "checkbox",
  // The publish pipeline writes dates as date_time millis.
  date: "date_time",
  // Image/URL columns are intentionally degraded to text on paste (see task P4 fix).
  image: "text",
  url: "text",
  unknown: "text",
};

const DOWNGRADE_NOTES: Partial<Record<SemanticFieldType, string>> = {
  long_text: "多行文本没有独立的腾讯列类型，已按文本列导入",
  boolean: "是/否字段已按复选框（checkbox）导入",
  date: "日期字段已按日期时间（date_time）导入",
  image: "图片/附件字段已按文本导入，不会自动写入图片",
  url: "链接字段已按文本导入",
};

export function mapTencentFieldType(raw: unknown): TypeMapping {
  if (raw === undefined || raw === null || String(raw).trim() === "") {
    return {
      semantic: "unknown",
      internal: "text",
      note: "缺少字段类型，已按文本导入",
    };
  }
  if (typeof raw === "number") {
    return {
      semantic: "unknown",
      internal: "text",
      note: `无法识别的字段类型编号 ${raw}，已按文本导入，请确认`,
    };
  }
  const normalized = String(raw).trim().toLowerCase().replace(/[\s-]+/g, "_");
  const withoutPrefix = normalized.startsWith("field_type_")
    ? normalized.slice("field_type_".length)
    : normalized;
  const simplified = withoutPrefix
    .replace(/[（(].*?[)）]/g, "")
    .replace(/(字段|类型)$/g, "")
    .trim();
  const semantic = TYPE_ALIASES[withoutPrefix]
    ?? TYPE_ALIASES[withoutPrefix.replace(/_/g, "")]
    ?? TYPE_ALIASES[simplified]
    ?? TYPE_ALIASES[simplified.replace(/_/g, "")];
  if (!semantic) {
    return {
      semantic: "unknown",
      internal: "text",
      note: `无法识别字段类型“${String(raw).trim()}”，已按文本导入，请确认`,
    };
  }
  return {
    semantic,
    internal: INTERNAL_BY_SEMANTIC[semantic],
    ...(DOWNGRADE_NOTES[semantic] ? { note: DOWNGRADE_NOTES[semantic] } : {}),
  };
}

export function schemaTypeLabel(type: string): string {
  return SMART_SHEET_FIELD_TYPE_LABELS[type] ?? type;
}

const isRecord = (value: unknown): value is Record<string, unknown> => Boolean(value)
  && typeof value === "object"
  && !Array.isArray(value);

/**
 * Small built-in glossary so common Chinese column titles still get a stable
 * ASCII field ID without shipping a full pinyin dependency. Unknown Chinese
 * titles fall back to `field_N`.
 */
const PINYIN_TERMS: Record<string, string> = {
  问题: "wenti",
  描述: "miaoshu",
  状态: "zhuangtai",
  类型: "leixing",
  分类: "fenlei",
  日期: "riqi",
  时间: "shijian",
  处理人: "chuliren",
  负责人: "fuzeren",
  来源: "laiyuan",
  备注: "beizhu",
  标题: "biaoti",
  内容: "neirong",
  名称: "mingcheng",
  链接: "lianjie",
  附件: "fujian",
  图片: "tupian",
  数量: "shuliang",
  金额: "jine",
  模块: "mokuai",
  客户: "kehu",
  订单: "dingdan",
  订单号: "dingdanhao",
  原因: "yuanyin",
  方案: "fangan",
  结论: "jielun",
  建议: "jianyi",
  优先级: "youxianji",
  严重程度: "yanzhongchengdu",
  影响范围: "yingxiangfanwei",
  发生时间: "fashengshijian",
  创建时间: "chuangjianshijian",
  更新时间: "gengxinshijian",
  解决方案: "jiejuefangan",
  原因分析: "yuanyinfenxi",
};

const PINYIN_KEYS = Object.keys(PINYIN_TERMS).sort((left, right) => right.length - left.length);

function pinyinSlug(title: string): string {
  const source = title.trim();
  if (!source) return "";
  const parts: string[] = [];
  let index = 0;
  while (index < source.length) {
    const rest = source.slice(index);
    const match = PINYIN_KEYS.find((key) => rest.startsWith(key));
    if (!match) {
      if (/[\s_\-·、，,。./（）()]/.test(source[index])) {
        index += 1;
        continue;
      }
      return "";
    }
    parts.push(PINYIN_TERMS[match]);
    index += match.length;
  }
  return parts.join("_");
}

const isArrayOfRecords = (value: unknown): value is unknown[] => Array.isArray(value);

/** Lowercase ASCII slug used when a pasted field has no usable ID. */
export function normalizeFieldKey(title: string): string {
  const direct = PINYIN_TERMS[title.trim()];
  if (direct) return direct;
  const slug = title
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_");
  if (slug) return /^[0-9]/.test(slug) ? `field_${slug}` : slug;
  return pinyinSlug(title);
}

function firstText(definition: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = definition[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number") return String(value);
  }
  return "";
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}

function collectOptionValues(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const options: string[] = [];
  for (const item of value) {
    if (typeof item === "string" || typeof item === "number") {
      const text = String(item).trim();
      if (text) options.push(text);
      continue;
    }
    if (isRecord(item)) {
      const text = firstText(item, ["name", "title", "value", "text", "label"]);
      if (text) options.push(text);
    }
  }
  return [...new Set(options)];
}

function extractOptions(definition: Record<string, unknown>): string[] {
  const candidates: unknown[] = [
    definition.options,
    definition.choices,
    definition.items,
    definition.enum,
    recordValue(definition.property)?.options,
    recordValue(definition.property)?.choices,
    recordValue(definition.property)?.items,
    recordValue(definition.property)?.enum,
  ];
  for (const candidate of candidates) {
    const options = collectOptionValues(candidate);
    if (options.length) return options;
  }
  return [];
}

function fieldIdFrom(definition: Record<string, unknown>): string {
  return firstText(definition, ["id", "field_id", "fieldId", "key", "field_key", "fieldKey"]);
}

function titleFrom(definition: Record<string, unknown>): string {
  return firstText(definition, [
    "title",
    "name",
    "field_title",
    "fieldTitle",
    "field_name",
    "label",
    "column_name",
  ]);
}

function typeFrom(definition: Record<string, unknown>): unknown {
  const direct = definition.type ?? definition.field_type ?? definition.fieldType
    ?? definition.field_type_id ?? definition.fieldTypeId;
  if (direct !== undefined && direct !== null && String(direct).trim() !== "") return direct;
  const property = recordValue(definition.property);
  return property ? property.type ?? property.field_type : undefined;
}

function requiredFrom(definition: Record<string, unknown>, title: string): boolean {
  const raw = definition.required ?? definition.is_required ?? definition.isRequired
    ?? definition.must_fill ?? definition.mustFill;
  if (raw === true || raw === 1) return true;
  if (typeof raw === "string") {
    const normalized = raw.trim().toLowerCase();
    if (["true", "1", "yes", "y", "是", "required", "必填"].includes(normalized)) return true;
  }
  return isRequiredSchemaTitle(title);
}

function reserveKey(base: string, used: Set<string>): { key: string; renamed: boolean } {
  if (!used.has(base)) {
    used.add(base);
    return { key: base, renamed: false };
  }
  let suffix = 2;
  while (used.has(`${base}_${suffix}`)) suffix += 1;
  const key = `${base}_${suffix}`;
  used.add(key);
  return { key, renamed: true };
}

class MergeContext {
  readonly fields: ImportedField[] = [];
  readonly problems: SchemaImportProblem[] = [];
  readonly notes = new Set<string>();
  readonly usedIds = new Set<string>();
  readonly unknownTypes = new Map<string, string[]>();
  readonly downgrades = new Map<string, string[]>();
  fallbackIndex = 0;

  addField(
    rawDefinition: unknown,
    keyHint: string,
    fallbackTitleHint = "",
  ): void {
    const definition = isRecord(rawDefinition) ? rawDefinition : {};
    const looseTitle = typeof rawDefinition === "string" ? rawDefinition.trim() : "";
    const rawTitle = titleFrom(definition) || looseTitle || fallbackTitleHint;
    const rawType = typeFrom(definition);
    const mapping = mapTencentFieldType(rawType);
    const options = extractOptions(definition);
    const required = requiredFrom(definition, rawTitle);
    const baseTitle = stripRequiredSchemaTitle(rawTitle);

    this.fallbackIndex += 1;
    const suppliedId = (keyHint || fieldIdFrom(definition)).trim();
    const idBase = suppliedId || normalizeFieldKey(baseTitle) || `field_${this.fallbackIndex}`;
    const { key: fieldId, renamed } = reserveKey(idBase, this.usedIds);
    if (renamed) {
      this.problems.push({
        level: "warning",
        fieldId,
        message: `字段 ID“${idBase}”重复，已重命名为“${fieldId}”`,
      });
    }
    const title = baseTitle || `字段 ${fieldId}`;
    if (!baseTitle) {
      this.problems.push({
        level: "warning",
        fieldId,
        message: `字段“${fieldId}”缺少标题，已使用字段 ID 作为列名`,
      });
    }
    if (mapping.semantic === "unknown") {
      const list = this.unknownTypes.get(String(rawType ?? "")) ?? [];
      list.push(title);
      this.unknownTypes.set(String(rawType ?? ""), list);
    }
    if (mapping.note) {
      const key = mapping.note;
      const list = this.downgrades.get(key) ?? [];
      list.push(title);
      this.downgrades.set(key, list);
    }
    const internalTitle = withRequiredSchemaTitle(title, required);
    const field: ImportedField = {
      fieldId,
      title: internalTitle,
      type: mapping.internal,
      originalType: rawType === undefined || rawType === null ? "" : String(rawType),
      options,
      required,
    };
    this.fields.push(field);
    if (options.length && (mapping.internal === "single_select" || mapping.internal === "multiple_select")) {
      // Kept on the field so the schema builder can attach `enum`.
    }
  }

  addLooseTitle(title: string): void {
    this.addField({}, normalizeFieldKey(title), title);
  }
}

function parseFieldArray(items: unknown[], context: MergeContext): void {
  items.forEach((item, index) => {
    if (typeof item === "string" || typeof item === "number") {
      context.addLooseTitle(String(item).trim() || `字段 ${index + 1}`);
      return;
    }
    if (!isRecord(item)) return;
    context.addField(item, "");
  });
}

function looksLikeFieldDefinition(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const keys = ["title", "name", "field_title", "fieldTitle", "field_name", "label", "field_id", "fieldId", "type", "field_type", "fieldType"];
  return keys.some((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function looksLikeInternalSchema(value: Record<string, unknown>): boolean {
  const entries = Object.entries(value);
  if (!entries.length) return false;
  return entries.every(([, entry]) => isRecord(entry) || typeof entry === "string")
    && entries.some(([, entry]) => (isRecord(entry) && looksLikeFieldDefinition(entry))
      || typeof entry === "string");
}

const WRAPPER_KEYS = ["fields", "schema", "columns", "data", "field_definitions", "fieldDefinitions", "items"];

function parseValue(value: unknown, context: MergeContext, depth = 0): void {
  if (depth > 6) {
    throw new Error("Schema 嵌套层级过深，无法识别字段定义");
  }
  if (isArrayOfRecords(value)) {
    parseFieldArray(value, context);
    return;
  }
  if (!isRecord(value)) {
    throw new Error("Schema 顶层必须是字段数组或对象");
  }
  for (const wrapper of WRAPPER_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(value, wrapper)) continue;
    const child = value[wrapper];
    if (Array.isArray(child)) {
      parseFieldArray(child, context);
      return;
    }
    if (isRecord(child)) {
      // `{fields: {id: {...}}}` / `{schema: {...}}` style wrappers.
      if (looksLikeInternalSchema(child)) {
        parseInternalSchema(child, context);
      } else {
        parseValue(child, context, depth + 1);
      }
      return;
    }
    throw new Error(`字段 ${wrapper} 必须是字段数组或对象`);
  }
  if (looksLikeFieldDefinition(value)) {
    context.addField(value, "");
    return;
  }
  if (looksLikeInternalSchema(value)) {
    parseInternalSchema(value, context);
    return;
  }
  throw new Error(
    "没有识别到字段定义。支持内部对象 {字段ID:{title,type}}、字段数组 [{field_id,field_title,field_type}]，"
    + "以及 {fields|schema|columns|data:[...]} 包装。",
  );
}

function parseInternalSchema(value: Record<string, unknown>, context: MergeContext): void {
  for (const [fieldId, definition] of Object.entries(value)) {
    if (isRecord(definition)) {
      context.addField(definition, fieldId);
    } else if (typeof definition === "string") {
      context.addField({ title: definition }, fieldId);
    }
  }
}

function buildSchema(context: MergeContext): Record<string, SmartSheetFieldSchema> {
  const schema: Record<string, SmartSheetFieldSchema> = {};
  for (const field of context.fields) {
    const definition: SmartSheetFieldSchema = {
      title: field.title,
      type: field.type,
    };
    if (field.options.length) definition.enum = field.options;
    schema[field.fieldId] = definition;
  }
  return schema;
}

function finalize(context: MergeContext): SchemaImportResult {
  if (!context.fields.length) {
    throw new Error(
      "没有识别到任何字段。示例：{\"fields\":[{\"field_id\":\"f1\",\"field_title\":\"问题描述\",\"field_type\":\"text\"}]}",
    );
  }
  for (const [rawType, titles] of context.unknownTypes) {
    if (!rawType) continue;
    context.problems.push({
      level: "warning",
      message: `无法识别字段类型“${rawType}”，已按文本导入：${titles.slice(0, 5).join("、")}${titles.length > 5 ? " 等" : ""}`,
    });
  }
  for (const [note, titles] of context.downgrades) {
    context.notes.add(`${note}：${titles.slice(0, 5).join("、")}${titles.length > 5 ? " 等" : ""}`);
  }
  const typeCounts: Record<string, number> = {};
  for (const field of context.fields) {
    typeCounts[field.type] = (typeCounts[field.type] ?? 0) + 1;
  }
  return {
    schema: buildSchema(context),
    fields: context.fields,
    problems: context.problems,
    notes: [...context.notes],
    typeCounts,
  };
}

/** Parses one of the supported Tencent schema shapes into the internal schema. */
export function parseTencentSchema(text: string): SchemaImportResult {
  if (!text.trim()) {
    throw new Error("请先粘贴腾讯文档 Schema JSON。示例：{\"fields\":[{\"field_id\":\"f1\",\"field_title\":\"问题描述\",\"field_type\":\"text\"}]}");
  }
  if (text.length > 2_000_000) {
    throw new Error("Schema 过大（超过 2 MB），请只粘贴字段定义部分。");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    const detail = error instanceof Error ? error.message : "未知错误";
    throw new Error(`Schema JSON 不是有效 JSON（${detail}）。示例：{"fields":[{"field_id":"f1","field_title":"问题描述","field_type":"text"}]}`);
  }
  const context = new MergeContext();
  parseValue(parsed, context);
  return finalize(context);
}

/** Internal schema -> generic internal JSON (the advanced editor's format). */
export function exportSchemaAsGenericJson(schema: Record<string, SmartSheetFieldSchema>): string {
  return `${JSON.stringify(schema, null, 2)}\n`;
}

/** Internal schema -> Tencent-style field array that this parser can re-import. */
export function exportSchemaAsTencentFields(schema: Record<string, SmartSheetFieldSchema>): string {
  const fields = Object.entries(schema).map(([fieldId, definition]) => {
    const title = typeof definition.title === "string" ? definition.title : fieldId;
    const required = isRequiredSchemaTitle(title);
    const options = Array.isArray(definition.enum) ? definition.enum.map(String) : [];
    const field: Record<string, unknown> = {
      field_id: fieldId,
      field_title: stripRequiredSchemaTitle(title) || fieldId,
      field_type: typeof definition.type === "string" ? definition.type : "text",
    };
    if (required) field.required = true;
    if (options.length) field.options = options;
    return field;
  });
  return `${JSON.stringify({ fields }, null, 2)}\n`;
}

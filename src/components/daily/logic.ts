/** Pure helpers for the daily assistant pages (no React, no bridge access). */

export interface DateRange {
  startDate: string;
  endDate: string;
}

export interface SearchFilters {
  query: string;
  source: string;
  sender: string;
  from: string;
  to: string;
  kinds: string[];
}

export interface HighlightSegment {
  text: string;
  match: boolean;
}

export interface StatEntry {
  key: string;
  label: string;
  value: string;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const DAY_MS = 24 * 60 * 60 * 1000;

export const EQ_STYLE_ORDER = ["steady", "warm", "brief"] as const;

export const EQ_STYLE_LABELS: Record<string, string> = {
  steady: "稳重",
  warm: "亲和",
  brief: "简短",
};

export const SOURCE_LABELS: Record<string, string> = {
  wecom: "企业微信",
  wechat: "个人微信",
};

export const KIND_LABELS: Record<string, string> = {
  text: "文本",
  image: "图片",
  file: "文件",
  voice: "语音",
  link: "链接",
  quote: "引用",
};

export const WEEKLY_STAT_LABELS: Record<string, string> = {
  messages: "消息",
  conversations: "会话",
  groups: "群聊",
  contacts: "联系人",
  wecom: "企业微信消息",
  wechat: "个人微信消息",
  issues: "问题",
  replies: "回复",
  todos: "待办",
  total: "总计",
};

export function toIsoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function startOfWeek(date: Date): Date {
  const copy = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const offset = (copy.getDay() + 6) % 7; // Monday-based week
  copy.setDate(copy.getDate() - offset);
  return copy;
}

export function weekRange(now = new Date()): DateRange {
  return { startDate: toIsoDate(startOfWeek(now)), endDate: toIsoDate(now) };
}

export function lastWeekRange(now = new Date()): DateRange {
  const start = startOfWeek(now);
  const end = new Date(start.getTime() - DAY_MS);
  const lastStart = new Date(start.getTime() - 7 * DAY_MS);
  return { startDate: toIsoDate(lastStart), endDate: toIsoDate(end) };
}

export function lastNDaysRange(now = new Date(), days = 7): DateRange {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  start.setDate(start.getDate() - Math.max(1, days) + 1);
  return { startDate: toIsoDate(start), endDate: toIsoDate(now) };
}

export function isValidIsoDate(value: string): boolean {
  if (!ISO_DATE.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  return (
    date.getFullYear() === year &&
    date.getMonth() === month - 1 &&
    date.getDate() === day
  );
}

export function validateDateRange(startDate: string, endDate: string): string | null {
  if (!startDate || !endDate) return "请选择开始和结束日期";
  if (!isValidIsoDate(startDate) || !isValidIsoDate(endDate)) {
    return "日期格式不正确，请使用 YYYY-MM-DD";
  }
  if (startDate > endDate) return "开始日期不能晚于结束日期";
  return null;
}

export function dateRangeLabel(range: DateRange): string {
  return `${range.startDate} 至 ${range.endDate}`;
}

export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Splits text into match/non-match segments for safe JSX highlighting. */
export function highlightSegments(text: string, query: string): HighlightSegment[] {
  if (!text) return [];
  const terms = query
    .split(/\s+/)
    .map((term) => term.trim())
    .filter(Boolean)
    .slice(0, 8);
  if (!terms.length) return [{ text, match: false }];
  const matcher = new RegExp(`(${terms.map(escapeRegExp).join("|")})`, "gi");
  return text
    .split(matcher)
    .map((part, index) => ({ text: part, match: index % 2 === 1 }))
    .filter((segment) => segment.text !== "");
}

export function buildSearchPayload(
  filters: SearchFilters,
  limit: number,
  offset: number,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    limit: Math.max(1, Math.trunc(limit)),
    offset: Math.max(0, Math.trunc(offset)),
  };
  const query = filters.query.trim();
  if (query) payload.query = query;
  if (filters.source && filters.source !== "all") payload.source = filters.source;
  const sender = filters.sender.trim();
  if (sender) payload.sender = sender;
  if (filters.from) payload.from = filters.from;
  if (filters.to) payload.to = filters.to;
  if (filters.kinds.length) payload.kinds = [...filters.kinds];
  return payload;
}

export function normalizeTimestamp(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") {
    if (!Number.isFinite(value) || value <= 0) return null;
    return value < 1e12 ? value * 1000 : value;
  }
  const numeric = Number(value);
  if (Number.isFinite(numeric) && String(value).trim() !== "") {
    return numeric < 1e12 ? numeric * 1000 : numeric;
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

export function formatMessageTime(value: number | string | null | undefined): string {
  const ms = normalizeTimestamp(value);
  if (ms === null) return "";
  const date = new Date(ms);
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function sourceLabel(source: string | undefined): string {
  if (!source) return "未知来源";
  return SOURCE_LABELS[source] ?? source;
}

export function kindLabel(kind: string | undefined): string {
  if (!kind) return "文本";
  return KIND_LABELS[kind] ?? kind;
}

export function progressPercent(progress: number | undefined): number {
  if (!Number.isFinite(progress)) return 0;
  const value = (progress as number) <= 1 ? (progress as number) * 100 : (progress as number);
  return Math.max(0, Math.min(100, Math.round(value)));
}

export function formatStatValue(value: unknown): string {
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string") return value.trim();
  if (Array.isArray(value)) return String(value.length);
  return "";
}

export function statEntries(stats: Record<string, unknown> | null | undefined): StatEntry[] {
  if (!stats) return [];
  return Object.entries(stats)
    .flatMap(([key, value]) => {
      const text = formatStatValue(value);
      if (!text) return [];
      return [{ key, label: WEEKLY_STAT_LABELS[key] ?? key, value: text }];
    })
    .slice(0, 8);
}

export function sortEqStyles<T extends { id: string }>(styles: T[]): T[] {
  const order = EQ_STYLE_ORDER as readonly string[];
  return [...styles].sort((a, b) => {
    const left = order.indexOf(a.id);
    const right = order.indexOf(b.id);
    return (left < 0 ? 99 : left) - (right < 0 ? 99 : right);
  });
}

export function eqStyleLabel(id: string): string {
  return EQ_STYLE_LABELS[id] ?? id;
}

export interface WeeklyMarkdownInput {
  startDate: string;
  endDate: string;
  draft: string;
  stats: Record<string, unknown>;
}

export function composeWeeklyMarkdown(input: WeeklyMarkdownInput): string {
  const lines = [`# 周报 ${input.startDate} 至 ${input.endDate}`, ""];
  const stats = statEntries(input.stats);
  if (stats.length) {
    lines.push(`> 统计：${stats.map((entry) => `${entry.label} ${entry.value}`).join(" · ")}`, "");
  }
  lines.push(input.draft.trim() || "（暂无草稿，请先生成或手动填写。）");
  return lines.join("\n");
}

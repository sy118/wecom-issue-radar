/**
 * Pure helpers for the 沟通助手 pages: response normalization, variable
 * substitution, filtering and Markdown export. No React and no bridge access,
 * so every rule here is unit-testable without mocking Electron.
 */

import type {
  DigestDrafts,
  DigestMode,
  EqCheck,
  EqInterpretation,
  EqReplyResult,
  EqSignal,
  EqStyle,
  EqStyleId,
  Phrase,
  PhraseDraft,
  SummarizeResult,
} from "./types";

export const EQ_STYLE_ORDER: readonly EqStyleId[] = ["steady", "warm", "brief"];

export const EQ_STYLE_LABELS: Record<string, string> = {
  steady: "稳重",
  warm: "亲和",
  brief: "简短",
};

export const DIGEST_MODE_LABELS: Record<DigestMode, string> = {
  digest: "长文速读",
  meeting: "会议纪要",
};

export const DIGEST_MODE_HINTS: Record<DigestMode, string> = {
  digest: "聊天长记录、公告、邮件：提炼结论与要点。",
  meeting: "会议记录、访谈：提炼结论、待办与风险。",
};

/** Variables the 话术库 can fill without asking the backend. */
export const PHRASE_VARIABLE_NAMES = ["姓名", "日期", "金额"] as const;

/** Fallback recipient label used when the user leaves 称呼 empty. */
export const DEFAULT_RECIPIENT = "对方";

const VARIABLE_PATTERN = /\{\{\s*([^{}\s][^{}]*?)\s*\}\}/g;

export type VariableValues = Record<string, string>;

export function todayDate(now: Date = new Date()): string {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** Default values for the built-in variables; `日期` tracks the local date. */
export function defaultVariableValues(today: string = todayDate()): VariableValues {
  return { 日期: today, 姓名: "", 金额: "" };
}

export function trimText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function stringList(value: unknown): string[] {
  if (typeof value === "string") {
    return value
      .split(/\r?\n/)
      .map((line) => line.replace(/^[-*·\d.、)\s]+/, "").trim())
      .filter(Boolean);
  }
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (typeof item === "string") return item.trim();
      if (item && typeof item === "object") {
        const record = item as Record<string, unknown>;
        const text = trimText(record.text) || trimText(record.detail) || trimText(record.title);
        return text;
      }
      return "";
    })
    .filter(Boolean);
}

/* ------------------------------------------------------------------ */
/* eq_reply                                                            */
/* ------------------------------------------------------------------ */

export function eqReplyPayload(input: {
  text: string;
  context?: string;
  recipient?: string;
}): Record<string, unknown> {
  const payload: Record<string, unknown> = { text: input.text.trim() };
  const context = trimText(input.context);
  if (context) payload.context = context;
  const recipient = trimText(input.recipient);
  if (recipient) payload.recipient = recipient;
  return payload;
}

export function summarizePayload(text: string, mode: DigestMode): Record<string, unknown> {
  return { text: text.trim(), mode };
}

function normalizeStyle(raw: unknown, fallbackId: EqStyleId): EqStyle | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const content = trimText(record.content);
  if (!content) return null;
  const id = trimText(record.id) || fallbackId;
  const title = trimText(record.title) || EQ_STYLE_LABELS[id] || id;
  return { id, title, content };
}

function normalizeSignals(value: unknown): EqSignal[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const record = item as Record<string, unknown>;
      const label = trimText(record.label);
      const detail = trimText(record.detail);
      if (!label && !detail) return null;
      return { label: label || "信号", detail };
    })
    .filter((item): item is EqSignal => item !== null);
}

function normalizeCheck(value: unknown): EqCheck {
  const record = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  return {
    risks: stringList(record.risks),
    suggestions: stringList(record.suggestions),
  };
}

function normalizeInterpretation(value: unknown): EqInterpretation {
  const record = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  return {
    summary: trimText(record.summary),
    signals: normalizeSignals(record.signals),
  };
}

/** Orders the three styles as 稳重/亲和/简短 and drops empty ones. */
export function sortEqStyles<T extends { id: string }>(styles: T[]): T[] {
  const order = EQ_STYLE_ORDER as readonly string[];
  return [...styles].sort((left, right) => {
    const leftIndex = order.indexOf(left.id);
    const rightIndex = order.indexOf(right.id);
    return (leftIndex < 0 ? 99 : leftIndex) - (rightIndex < 0 ? 99 : rightIndex);
  });
}

export function eqStyleLabel(id: string): string {
  return EQ_STYLE_LABELS[id] ?? id;
}

/** Defensive parser for the `eq_reply` response. */
export function normalizeEqResult(raw: unknown): EqReplyResult {
  const record = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const seen = new Set<string>();
  const styles: EqStyle[] = [];
  if (Array.isArray(record.styles)) {
    for (const item of record.styles) {
      const style = normalizeStyle(item, "steady");
      if (!style || seen.has(style.id)) continue;
      seen.add(style.id);
      styles.push(style);
    }
  }
  return {
    styles: sortEqStyles(styles),
    interpretation: normalizeInterpretation(record.interpretation),
    check: normalizeCheck(record.check),
    fallback: Boolean(record.fallback),
  };
}

/** Initial editable drafts for the three style cards. */
export function replyDrafts(result: EqReplyResult | null): Record<string, string> {
  const drafts: Record<string, string> = {};
  for (const style of result?.styles ?? []) drafts[style.id] = style.content;
  return drafts;
}

/* ------------------------------------------------------------------ */
/* summarize                                                           */
/* ------------------------------------------------------------------ */

/** Defensive parser for the `summarize` response. */
export function normalizeSummarize(raw: unknown): SummarizeResult {
  const record = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  return {
    summary: trimText(record.summary),
    points: stringList(record.points),
    risks: stringList(record.risks),
    next: stringList(record.next),
    fallback: Boolean(record.fallback),
  };
}

export function digestDrafts(result: SummarizeResult | null): DigestDrafts {
  return {
    summary: result?.summary ?? "",
    points: (result?.points ?? []).join("\n"),
    risks: (result?.risks ?? []).join("\n"),
    next: (result?.next ?? []).join("\n"),
  };
}

/** Splits a textarea (one item per line) into trimmed, non-empty lines. */
export function parseLines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.replace(/^[-*·\d.、)\s]+/, "").trim())
    .filter(Boolean);
}

export function draftsToSummarize(value: DigestDrafts): SummarizeResult {
  return {
    summary: value.summary.trim(),
    points: parseLines(value.points),
    risks: parseLines(value.risks),
    next: parseLines(value.next),
    fallback: false,
  };
}

export function digestMarkdownFilename(mode: DigestMode, today: string = todayDate()): string {
  return `速读-${mode === "meeting" ? "会议" : "长文"}-${today}.md`;
}

/** Renders the current (possibly edited) digest as Markdown. */
export function composeDigestMarkdown(input: {
  mode: DigestMode;
  drafts: DigestDrafts;
  today?: string;
}): string {
  const today = input.today ?? todayDate();
  const title = input.mode === "meeting" ? "会议纪要" : "长文速读";
  const lines = [`# ${title} · ${today}`, ""];
  const summary = input.drafts.summary.trim();
  lines.push("## 摘要", "", summary || "（暂无摘要）", "");
  const sections: Array<[string, string[]]> = [
    ["要点", parseLines(input.drafts.points)],
    ["风险", parseLines(input.drafts.risks)],
    ["下一步", parseLines(input.drafts.next)],
  ];
  for (const [heading, items] of sections) {
    lines.push(`## ${heading}`, "");
    if (items.length === 0) lines.push("（暂无）");
    else for (const item of items) lines.push(`- ${item}`);
    lines.push("");
  }
  return lines.join("\n").trimEnd() + "\n";
}

/* ------------------------------------------------------------------ */
/* phrase variables                                                    */
/* ------------------------------------------------------------------ */

export function extractVariables(content: string): string[] {
  const found: string[] = [];
  for (const match of content.matchAll(VARIABLE_PATTERN)) {
    const name = match[1].trim();
    if (name && !found.includes(name)) found.push(name);
  }
  return found;
}

/** Replaces `{{姓名}}`-style placeholders; unknown names stay untouched. */
export function applyVariables(
  content: string,
  values: VariableValues,
  today: string = todayDate(),
): string {
  return content.replace(VARIABLE_PATTERN, (whole, rawName: string) => {
    const name = rawName.trim();
    if (name === "日期" && !trimText(values.日期)) return today;
    const value = trimText(values[name]);
    return value || whole;
  });
}

/** Placeholders still lacking a value, so the page can warn before copying. */
export function missingVariables(content: string, values: VariableValues): string[] {
  return extractVariables(content).filter((name) => {
    if (name === "日期") return false;
    return !trimText(values[name]);
  });
}

/* ------------------------------------------------------------------ */
/* phrases                                                             */
/* ------------------------------------------------------------------ */

function normalizeVariables(value: unknown, content: string): string[] {
  const fromPayload = Array.isArray(value)
    ? value.map((item) => trimText(item)).filter(Boolean)
    : [];
  const merged = [...fromPayload];
  for (const name of extractVariables(content)) {
    if (!merged.includes(name)) merged.push(name);
  }
  return merged;
}

export function normalizePhrase(raw: unknown): Phrase | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const id = trimText(record.id);
  const content = trimText(record.content);
  if (!id || !content) return null;
  return {
    id,
    title: trimText(record.title) || "未命名话术",
    category: trimText(record.category) || "未分类",
    content,
    variables: normalizeVariables(record.variables, content),
  };
}

export function normalizePhrases(raw: unknown): Phrase[] {
  const record = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const items = Array.isArray(record.phrases) ? record.phrases : [];
  return items
    .map((item) => normalizePhrase(item))
    .filter((item): item is Phrase => item !== null);
}

export function phraseCategories(phrases: Phrase[]): string[] {
  const categories: string[] = [];
  for (const phrase of phrases) {
    if (phrase.category && !categories.includes(phrase.category)) categories.push(phrase.category);
  }
  return categories;
}

/** Keyword match over title/category/content, optionally constrained by category. */
export function filterPhrases(phrases: Phrase[], query: string, category: string): Phrase[] {
  const keyword = query.trim().toLowerCase();
  return phrases.filter((phrase) => {
    if (category && category !== "全部" && phrase.category !== category) return false;
    if (!keyword) return true;
    return (
      phrase.title.toLowerCase().includes(keyword)
      || phrase.content.toLowerCase().includes(keyword)
      || phrase.category.toLowerCase().includes(keyword)
    );
  });
}

export function emptyPhraseDraft(): PhraseDraft {
  return { title: "", category: "通用", content: "" };
}

export function draftFromPhrase(phrase: Phrase): PhraseDraft {
  return { id: phrase.id, title: phrase.title, category: phrase.category, content: phrase.content };
}

export function validatePhraseDraft(draft: PhraseDraft): string | null {
  if (!draft.title.trim()) return "请填写话术标题";
  if (!draft.content.trim()) return "请填写话术内容";
  if (draft.content.trim().length > 2000) return "话术内容请控制在 2000 字以内";
  return null;
}

/**
 * Replaces a saved phrase in place (the backend already returns title order) and
 * appends new ones, so the list never jumps around while the user edits.
 */
export function upsertPhrase(phrases: Phrase[], phrase: Phrase): Phrase[] {
  const index = phrases.findIndex((item) => item.id === phrase.id);
  if (index < 0) return [...phrases, phrase];
  return phrases.map((item, itemIndex) => (itemIndex === index ? phrase : item));
}

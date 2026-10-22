import { describe, expect, it } from "vitest";

import {
  EQ_STYLE_LABELS,
  applyVariables,
  composeDigestMarkdown,
  defaultVariableValues,
  digestDrafts,
  digestMarkdownFilename,
  draftFromPhrase,
  draftsToSummarize,
  emptyPhraseDraft,
  eqReplyPayload,
  eqStyleLabel,
  extractVariables,
  filterPhrases,
  missingVariables,
  normalizeEqResult,
  normalizePhrases,
  normalizeSummarize,
  parseLines,
  phraseCategories,
  replyDrafts,
  sortEqStyles,
  summarizePayload,
  todayDate,
  upsertPhrase,
  validatePhraseDraft,
} from "./logic";
import type { Phrase } from "./types";

const EQ_RAW = {
  styles: [
    { id: "brief", title: "简短", content: "收到，今天下班前给你。" },
    { id: "steady", title: "稳重", content: "收到，我先确认一下排期，今天 18:00 前给结论。" },
    { id: "warm", title: "亲和", content: "理解你的着急，我这就去看，尽快回你。" },
    { id: "steady", title: "重复", content: "重复项应被丢弃" },
    { id: "warm", title: "空的", content: "   " },
  ],
  interpretation: {
    summary: "对方在催进度，情绪偏急。",
    signals: [
      { label: "催促", detail: "连续追问时间点" },
      { label: "", detail: "" },
    ],
  },
  check: { risks: ["不要把责任推给同事"], suggestions: ["给出明确时间"] },
  fallback: false,
};

describe("eq_reply payload", () => {
  it("trims the message and omits empty optional fields", () => {
    expect(eqReplyPayload({ text: "  在吗  ", context: "  ", recipient: "" })).toEqual({ text: "在吗" });
  });

  it("keeps context and recipient when provided", () => {
    expect(eqReplyPayload({ text: "在吗", context: "上周的报价", recipient: "张总" })).toEqual({
      text: "在吗",
      context: "上周的报价",
      recipient: "张总",
    });
  });
});

describe("eq_reply normalization", () => {
  it("orders styles as 稳重/亲和/简短 and drops empty or duplicate ones", () => {
    const result = normalizeEqResult(EQ_RAW);
    expect(result.styles.map((style) => style.id)).toEqual(["steady", "warm", "brief"]);
    expect(result.styles[0].content).toContain("18:00");
    expect(result.fallback).toBe(false);
  });

  it("keeps the type contract when the backend returns almost nothing", () => {
    const result = normalizeEqResult({ styles: "nope", interpretation: null, check: [], fallback: true });
    expect(result).toEqual({
      styles: [],
      interpretation: { summary: "", signals: [] },
      check: { risks: [], suggestions: [] },
      fallback: true,
    });
  });

  it("normalizes interpretation signals and check lists", () => {
    const result = normalizeEqResult(EQ_RAW);
    expect(result.interpretation.summary).toContain("催进度");
    expect(result.interpretation.signals).toEqual([{ label: "催促", detail: "连续追问时间点" }]);
    expect(result.check.risks).toEqual(["不要把责任推给同事"]);
    expect(result.check.suggestions).toEqual(["给出明确时间"]);
  });

  it("labels unknown styles by id and exposes drafts for editing", () => {
    expect(eqStyleLabel("steady")).toBe("稳重");
    expect(eqStyleLabel("poetic")).toBe("poetic");
    expect(EQ_STYLE_LABELS.brief).toBe("简短");
    expect(sortEqStyles([{ id: "brief" }, { id: "warm" }]).map((item) => item.id)).toEqual(["warm", "brief"]);
    expect(replyDrafts(normalizeEqResult(EQ_RAW)).steady).toContain("18:00");
  });
});

describe("summarize normalization", () => {
  it("accepts arrays and bullet strings", () => {
    const result = normalizeSummarize({
      summary: "  结论  ",
      points: ["一", " 二 "],
      risks: "- 风险 A\n- 风险 B",
      next: ["找张总确认"],
      fallback: true,
    });
    expect(result.summary).toBe("结论");
    expect(result.points).toEqual(["一", "二"]);
    expect(result.risks).toEqual(["风险 A", "风险 B"]);
    expect(result.next).toEqual(["找张总确认"]);
    expect(result.fallback).toBe(true);
  });

  it("round-trips editable drafts and strips bullet prefixes", () => {
    const drafts = digestDrafts(normalizeSummarize({ summary: "s", points: ["a"], risks: [], next: [] }));
    expect(drafts).toEqual({ summary: "s", points: "a", risks: "", next: "" });
    const edited = { summary: " s2 ", points: "- a\n- b", risks: "", next: "1. c" };
    expect(draftsToSummarize(edited)).toMatchObject({
      summary: "s2",
      points: ["a", "b"],
      next: ["c"],
      fallback: false,
    });
    expect(parseLines("  \n")).toEqual([]);
  });

  it("builds a Markdown digest with empty sections and a dated filename", () => {
    const markdown = composeDigestMarkdown({
      mode: "meeting",
      today: "2026-10-22",
      drafts: { summary: "结论", points: "a\nb", risks: "", next: "c" },
    });
    expect(markdown).toContain("# 会议纪要 · 2026-10-22");
    expect(markdown).toContain("## 摘要\n\n结论");
    expect(markdown).toContain("- a\n- b");
    expect(markdown).toContain("## 风险\n\n（暂无）");
    expect(digestMarkdownFilename("digest", "2026-10-22")).toBe("速读-长文-2026-10-22.md");
    expect(digestMarkdownFilename("meeting", "2026-10-22")).toBe("速读-会议-2026-10-22.md");
    expect(summarizePayload("  长文 ", "digest")).toEqual({ text: "长文", mode: "digest" });
  });
});

describe("phrase variables", () => {
  it("extracts unique variables in order", () => {
    expect(extractVariables("{{姓名}}好，{{ 日期 }}要{{金额}}元，再{{姓名}}")).toEqual(["姓名", "日期", "金额"]);
    expect(extractVariables("没有变量")).toEqual([]);
  });

  it("applies known values, defaults 日期 to today, and leaves unknown names untouched", () => {
    expect(applyVariables("{{姓名}}，{{日期}}，{{金额}}，{{部门}}", { 姓名: "张总", 金额: "100" }, "2026-10-22")).toBe(
      "张总，2026-10-22，100，{{部门}}",
    );
  });

  it("reports missing variables but never blocks 日期", () => {
    expect(missingVariables("{{姓名}}{{日期}}{{金额}}", { 姓名: "", 金额: "", 日期: "" })).toEqual(["姓名", "金额"]);
    expect(defaultVariableValues("2026-10-22")).toEqual({ 日期: "2026-10-22", 姓名: "", 金额: "" });
    expect(todayDate(new Date(2026, 9, 22, 23, 0))).toBe("2026-10-22");
  });
});

describe("phrase collection", () => {
  const phrases: Phrase[] = [
    { id: "b", title: "请假", category: "请假", content: "{{姓名}}请一天假", variables: ["姓名"] },
    { id: "a", title: "催进度", category: "协作", content: "方便同步一下进度吗", variables: [] },
    { id: "c", title: "道歉", category: "协作", content: "抱歉，这次是我没对齐", variables: [] },
  ];

  it("filters by keyword and category", () => {
    expect(filterPhrases(phrases, "道歉", "全部").map((item) => item.id)).toEqual(["c"]);
    expect(filterPhrases(phrases, "", "协作").map((item) => item.id)).toEqual(["a", "c"]);
    expect(filterPhrases(phrases, "进度", "请假")).toEqual([]);
  });

  it("collects categories in first-seen order", () => {
    expect(phraseCategories(phrases)).toEqual(["请假", "协作"]);
    expect(phraseCategories([])).toEqual([]);
  });

  it("validates drafts and replaces phrases in place", () => {
    expect(validatePhraseDraft(emptyPhraseDraft())).toBe("请填写话术标题");
    expect(validatePhraseDraft({ title: "t", category: "", content: "" })).toBe("请填写话术内容");
    expect(validatePhraseDraft({ title: "t", category: "", content: "c" })).toBeNull();
    const saved: Phrase = { id: "a", title: "AAA", category: "协作", content: "new", variables: [] };
    const next = upsertPhrase(phrases, saved);
    expect(next.map((item) => item.id)).toEqual(["b", "a", "c"]);
    expect(next[1].content).toBe("new");
    expect(upsertPhrase(phrases, { ...saved, id: "z" }).map((item) => item.id)).toEqual(["b", "a", "c", "z"]);
    expect(draftFromPhrase(phrases[0])).toMatchObject({ id: "b", title: "请假" });
  });

  it("normalizes malformed list payloads", () => {
    expect(normalizePhrases(null)).toEqual([]);
    expect(
      normalizePhrases({
        phrases: [
          { id: "x", title: "标题", category: "", content: "用{{金额}}结账" },
          { id: "", content: "没有 id" },
          "garbage",
        ],
      }),
    ).toEqual([
      { id: "x", title: "标题", category: "未分类", content: "用{{金额}}结账", variables: ["金额"] },
    ]);
  });
});

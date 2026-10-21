import { describe, expect, it } from "vitest";

import {
  buildSearchPayload,
  composeWeeklyMarkdown,
  dateRangeLabel,
  formatMessageTime,
  formatStatValue,
  highlightSegments,
  isValidIsoDate,
  kindLabel,
  lastNDaysRange,
  lastWeekRange,
  normalizeTimestamp,
  progressPercent,
  sortEqStyles,
  sourceLabel,
  statEntries,
  toIsoDate,
  validateDateRange,
  weekRange,
} from "./logic";

const WEDNESDAY = new Date(2026, 9, 21, 14, 30); // 2026-10-21
const SUNDAY = new Date(2026, 9, 25, 9, 0); // 2026-10-25

describe("daily date ranges", () => {
  it("uses a Monday-based week ending today", () => {
    expect(weekRange(WEDNESDAY)).toEqual({ startDate: "2026-10-19", endDate: "2026-10-21" });
    expect(weekRange(SUNDAY)).toEqual({ startDate: "2026-10-19", endDate: "2026-10-25" });
    expect(toIsoDate(new Date(2026, 0, 5))).toBe("2026-01-05");
  });

  it("returns the previous Monday-Sunday window", () => {
    expect(lastWeekRange(WEDNESDAY)).toEqual({ startDate: "2026-10-12", endDate: "2026-10-18" });
  });

  it("returns an inclusive N-day window", () => {
    expect(lastNDaysRange(WEDNESDAY, 7)).toEqual({ startDate: "2026-10-15", endDate: "2026-10-21" });
    expect(lastNDaysRange(WEDNESDAY, 1)).toEqual({ startDate: "2026-10-21", endDate: "2026-10-21" });
  });

  it("validates real calendar dates and reversed ranges", () => {
    expect(isValidIsoDate("2026-02-28")).toBe(true);
    expect(isValidIsoDate("2026-02-30")).toBe(false);
    expect(isValidIsoDate("2026-2-3")).toBe(false);
    expect(validateDateRange("2026-10-19", "2026-10-21")).toBeNull();
    expect(validateDateRange("2026-10-21", "2026-10-19")).toBe("开始日期不能晚于结束日期");
    expect(validateDateRange("", "2026-10-21")).toBe("请选择开始和结束日期");
    expect(validateDateRange("2026-02-30", "2026-03-01")).toBe("日期格式不正确，请使用 YYYY-MM-DD");
    expect(dateRangeLabel({ startDate: "2026-10-19", endDate: "2026-10-21" })).toBe("2026-10-19 至 2026-10-21");
  });
});

describe("search helpers", () => {
  it("splits matching terms into highlight segments without losing text", () => {
    const segments = highlightSegments("我要退款，退款流程太慢", "退款");
    expect(segments.map((segment) => segment.text).join("")).toBe("我要退款，退款流程太慢");
    expect(segments.filter((segment) => segment.match).map((segment) => segment.text)).toEqual(["退款", "退款"]);
  });

  it("highlights case-insensitively and supports multiple terms", () => {
    const english = highlightSegments("Refund REFUND now", "refund");
    expect(english.filter((segment) => segment.match)).toHaveLength(2);
    const mixed = highlightSegments("退款问题请联系客服", "退款 客服");
    expect(mixed.filter((segment) => segment.match).map((segment) => segment.text)).toEqual(["退款", "客服"]);
  });

  it("treats regex metacharacters literally and returns the whole text for an empty query", () => {
    const segments = highlightSegments("价格 C++ 合理吗", "C++");
    expect(segments.filter((segment) => segment.match).map((segment) => segment.text)).toEqual(["C++"]);
    expect(highlightSegments("没有关键词", " ")).toEqual([{ text: "没有关键词", match: false }]);
  });

  it("builds a compact search payload and strips empty filters", () => {
    expect(
      buildSearchPayload(
        { query: "  退款 ", source: "all", sender: " ", from: "", to: "", kinds: [] },
        30,
        60,
      ),
    ).toEqual({ limit: 30, offset: 60, query: "退款" });
    expect(
      buildSearchPayload(
        { query: "", source: "wecom", sender: "张三", from: "2026-10-01", to: "2026-10-21", kinds: ["text", "image"] },
        20,
        0,
      ),
    ).toEqual({
      limit: 20,
      offset: 0,
      source: "wecom",
      sender: "张三",
      from: "2026-10-01",
      to: "2026-10-21",
      kinds: ["text", "image"],
    });
  });

  it("normalizes seconds, milliseconds and ISO timestamps", () => {
    const local = new Date(2026, 9, 21, 9, 5, 0).getTime();
    expect(normalizeTimestamp(Math.floor(local / 1000))).toBe(Math.floor(local / 1000) * 1000);
    expect(formatMessageTime(local)).toBe("2026-10-21 09:05");
    expect(formatMessageTime("2026-10-21T09:05:00+08:00")).toBe("2026-10-21 09:05");
    expect(formatMessageTime("")).toBe("");
    expect(formatMessageTime("not-a-date")).toBe("");
  });

  it("clamps index progress and labels sources/kinds", () => {
    expect(progressPercent(0.42)).toBe(42);
    expect(progressPercent(55)).toBe(55);
    expect(progressPercent(120)).toBe(100);
    expect(progressPercent(Number.NaN)).toBe(0);
    expect(sourceLabel("wecom")).toBe("企业微信");
    expect(sourceLabel("wechat")).toBe("个人微信");
    expect(sourceLabel(undefined)).toBe("未知来源");
    expect(kindLabel("image")).toBe("图片");
    expect(kindLabel("reaction")).toBe("reaction");
  });
});

describe("report helpers", () => {
  it("formats stats and skips empty values", () => {
    const entries = statEntries({ messages: 128, conversations: 6, empty: "", ratio: 0.5, nested: { a: 1 } });
    expect(entries).toEqual([
      { key: "messages", label: "消息", value: "128" },
      { key: "conversations", label: "会话", value: "6" },
      { key: "ratio", label: "ratio", value: "0.5" },
    ]);
    expect(formatStatValue(true)).toBe("是");
    expect(formatStatValue(["a", "b"])).toBe("2");
  });

  it("composes an exportable weekly markdown", () => {
    const markdown = composeWeeklyMarkdown({
      startDate: "2026-10-19",
      endDate: "2026-10-21",
      draft: "## 本周进展\n- 修复登录问题",
      stats: { messages: 42, conversations: 3 },
    });
    expect(markdown).toContain("# 周报 2026-10-19 至 2026-10-21");
    expect(markdown).toContain("> 统计：消息 42 · 会话 3");
    expect(markdown).toContain("修复登录问题");
    expect(
      composeWeeklyMarkdown({ startDate: "2026-10-19", endDate: "2026-10-21", draft: "  ", stats: {} }),
    ).toContain("（暂无草稿，请先生成或手动填写。）");
  });

  it("orders the three reply styles and keeps unknown styles last", () => {
    const sorted = sortEqStyles([
      { id: "brief", title: "简短" },
      { id: "custom", title: "自定义" },
      { id: "steady", title: "稳重" },
      { id: "warm", title: "亲和" },
    ]);
    expect(sorted.map((style) => style.id)).toEqual(["steady", "warm", "brief", "custom"]);
  });
});

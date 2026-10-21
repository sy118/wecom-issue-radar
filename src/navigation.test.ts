import { describe, expect, it } from "vitest";
import {
  NAV_SECTIONS,
  defaultPageForSection,
  pageLabel,
  sectionForPage,
} from "./navigation";
import type { PageId } from "./types";

describe("top-level navigation", () => {
  it("keeps every page in exactly one section", () => {
    const seen = new Set<PageId>();
    for (const section of NAV_SECTIONS) {
      for (const page of section.pages) {
        expect(seen.has(page.id), `${page.id} appears twice`).toBe(false);
        seen.add(page.id);
        expect(sectionForPage(page.id).id).toBe(section.id);
      }
    }
  });

  it("separates WeCom and personal WeChat pages", () => {
    const wecom = NAV_SECTIONS.find((section) => section.id === "wecom");
    const wechat = NAV_SECTIONS.find((section) => section.id === "wechat");
    expect(wecom).toBeDefined();
    expect(wechat).toBeDefined();
    for (const page of wecom?.pages ?? []) expect(page.id.startsWith("wecom-")).toBe(true);
    for (const page of wechat?.pages ?? []) expect(page.id.startsWith("wechat-")).toBe(true);
  });

  it("routes sections to their first page and labels pages for the title", () => {
    for (const section of NAV_SECTIONS) {
      const page = defaultPageForSection(section);
      expect(pageLabel(page)).toBe(
        section.pages.find((item) => item.id === page)?.label ?? "工作台",
      );
    }
  });
});

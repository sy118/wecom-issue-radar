import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/** Guard rails for the three 沟通助手 pages (no DOM renderer in this repo). */

function readPage(name: string): string {
  return readFileSync(fileURLToPath(new URL(`./${name}`, import.meta.url)), "utf8");
}

const reply = readPage("DailyReplyPage.tsx");
const phrases = readPage("DailyPhrasesPage.tsx");
const digest = readPage("DailyDigestPage.tsx");

describe("DailyReplyPage", () => {
  it("exports the page and loads the scoped stylesheet", () => {
    expect(reply).toContain("export function DailyReplyPage");
    expect(reply).toContain('import "./daily.css"');
  });

  it("goes through the typed daily wrappers instead of raw bridge calls", () => {
    expect(reply).toContain("generateEqReply");
    expect(reply).toContain("fillWechat");
    expect(reply).toContain("normalizeEqResult");
    expect(reply).not.toContain('dailyAction("');
  });

  it("renders interpretation, three styles and the pre-send check with a safety note", () => {
    expect(reply).toContain("潜台词解读");
    expect(reply).toContain("发送前检查");
    expect(reply).toContain("三版回复");
    expect(reply).toContain("不会点击发送");
  });
});

describe("DailyPhrasesPage", () => {
  it("exports the page and loads the scoped stylesheet", () => {
    expect(phrases).toContain("export function DailyPhrasesPage");
    expect(phrases).toContain('import "./daily.css"');
  });

  it("covers list/save/delete plus variable substitution and fill", () => {
    expect(phrases).toContain("listPhrases");
    expect(phrases).toContain("savePhrase");
    expect(phrases).toContain("deletePhrase");
    expect(phrases).toContain("PHRASE_VARIABLE_NAMES");
    expect(phrases).toContain("applyVariables");
    expect(phrases).toContain("fillWechat");
  });

  it("uses a two-step delete instead of window.confirm", () => {
    expect(phrases).toContain("确认删除");
    expect(phrases).not.toContain("window.confirm");
    expect(phrases).not.toContain("alert(");
  });
});

describe("DailyDigestPage", () => {
  it("exports the page and loads the scoped stylesheet", () => {
    expect(digest).toContain("export function DailyDigestPage");
    expect(digest).toContain('import "./daily.css"');
  });

  it("supports both modes and Markdown export", () => {
    expect(digest).toContain("summarizeText");
    expect(digest).toContain("DIGEST_MODE_LABELS");
    expect(digest).toContain("composeDigestMarkdown");
    expect(digest).toContain("downloadMarkdown");
    expect(digest).toContain("导出 Markdown");
  });

  it("keeps the digest output editable", () => {
    expect(digest).toContain("update(\"summary\"");
    expect(digest).toContain("update(\"points\"");
    expect(digest).toContain("update(\"risks\"");
    expect(digest).toContain("update(\"next\"");
  });
});

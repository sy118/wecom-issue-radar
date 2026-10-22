import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  dailyAction: vi.fn(),
  wechatFillInput: vi.fn(),
}));

vi.mock("../../lib/bridge", () => ({ bridge: mocks }));

import {
  MISSING_DAILY_SERVICE,
  deletePhrase,
  fillWechat,
  generateEqReply,
  listPhrases,
  savePhrase,
  summarizeText,
  supportsWechatFill,
} from "./api";

beforeEach(() => {
  mocks.dailyAction.mockReset();
  mocks.wechatFillInput.mockReset();
});

describe("daily action wrappers", () => {
  it("calls eq_reply with only the provided fields", async () => {
    mocks.dailyAction.mockResolvedValue({ styles: [] });

    await generateEqReply({ text: " 在吗 " });
    expect(mocks.dailyAction).toHaveBeenCalledWith("eq_reply", { text: "在吗" });

    await generateEqReply({ text: "在吗", context: "报价", recipient: "张总" });
    expect(mocks.dailyAction).toHaveBeenLastCalledWith("eq_reply", {
      text: "在吗",
      context: "报价",
      recipient: "张总",
    });
  });

  it("calls summarize with the selected mode", async () => {
    mocks.dailyAction.mockResolvedValue({ summary: "" });
    await summarizeText("  长文 ", "meeting");
    expect(mocks.dailyAction).toHaveBeenCalledWith("summarize", { text: "长文", mode: "meeting" });
  });

  it("lists and normalizes phrases from the backend", async () => {
    mocks.dailyAction.mockResolvedValue({
      phrases: [{ id: "p1", title: "请假", category: "请假", content: "{{姓名}}请假", variables: ["姓名"] }],
    });
    const phrases = await listPhrases();
    expect(mocks.dailyAction).toHaveBeenCalledWith("phrases_list", {});
    expect(phrases).toHaveLength(1);
    expect(phrases[0].title).toBe("请假");
  });

  it("saves a new phrase without id and an existing one with id", async () => {
    mocks.dailyAction.mockResolvedValue({ phrase: { id: "p1", title: "t", category: "c", content: "body" } });

    const created = await savePhrase({ title: " t ", category: " c ", content: " body " });
    expect(mocks.dailyAction).toHaveBeenCalledWith("phrase_save", {
      phrase: { title: "t", category: "c", content: "body" },
    });
    expect(created.id).toBe("p1");

    await savePhrase({ id: "p1", title: "t", category: "c", content: "body" });
    expect(mocks.dailyAction).toHaveBeenLastCalledWith("phrase_save", {
      phrase: { id: "p1", title: "t", category: "c", content: "body" },
    });
  });

  it("falls back to 未分类 when the category is blank", async () => {
    mocks.dailyAction.mockResolvedValue({ phrase: { id: "p1", title: "t", category: "未分类", content: "body" } });
    await savePhrase({ title: "t", category: "   ", content: "body" });
    expect(mocks.dailyAction).toHaveBeenCalledWith("phrase_save", {
      phrase: { title: "t", category: "未分类", content: "body" },
    });
  });

  it("rejects an unusable save response instead of showing blank rows", async () => {
    mocks.dailyAction.mockResolvedValue({ phrase: { id: "" } });
    await expect(savePhrase({ title: "t", category: "c", content: "body" })).rejects.toThrow(
      "返回内容无法识别",
    );
  });

  it("deletes by id", async () => {
    mocks.dailyAction.mockResolvedValue({ ok: true });
    await deletePhrase("p1");
    expect(mocks.dailyAction).toHaveBeenCalledWith("phrase_delete", { id: "p1" });
  });

  it("surfaces a readable error when the daily service is unavailable", async () => {
    const original = mocks.dailyAction;
    // Simulate an older preload: the method is missing entirely.
    (mocks as { dailyAction?: unknown }).dailyAction = undefined;
    try {
      await expect(listPhrases()).rejects.toThrow(MISSING_DAILY_SERVICE);
    } finally {
      (mocks as { dailyAction?: unknown }).dailyAction = original;
    }
  });
});

describe("wechat fill wrappers", () => {
  it("delegates to wechatFillInput and reports availability", async () => {
    mocks.wechatFillInput.mockResolvedValue({ filled: true, chatTitle: "张总", characters: 8 });
    expect(supportsWechatFill()).toBe(true);
    await expect(fillWechat("张总", "收到马上处理")).resolves.toMatchObject({ filled: true });
    expect(mocks.wechatFillInput).toHaveBeenCalledWith("张总", "收到马上处理");
  });

  it("throws a readable error when fill is unsupported", async () => {
    const original = mocks.wechatFillInput;
    (mocks as { wechatFillInput?: unknown }).wechatFillInput = undefined;
    try {
      expect(supportsWechatFill()).toBe(false);
      await expect(fillWechat("张总", "x")).rejects.toThrow("不支持填入微信");
    } finally {
      (mocks as { wechatFillInput?: unknown }).wechatFillInput = original;
    }
  });
});

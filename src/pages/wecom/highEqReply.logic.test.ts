import {
  chatDisplayName,
  contentTypeLabel,
  messageDisplayText,
  sanitizeMessageText,
  senderDisplayName,
  shouldCollapseMessage,
} from "./highEqReply.logic";
import { describe, expect, it } from "vitest";

import {
  CONTEXT_LIMIT,
  CONTEXT_WINDOW_SECONDS,
  OCR_BATCH_LIMIT,
  OCR_EMPTY_PLACEHOLDER,
  OCR_FAILED_PLACEHOLDER,
  OCR_MAX_MESSAGE_ATTEMPTS,
  OCR_TIMEOUT_PLACEHOLDER,
  POLL_INTERVAL_MS,
  chatsFingerprint,
  composeContextText,
  contextFingerprint,
  defaultChatId,
  eqReplyPayload,
  eqStyleLabel,
  formatChatTime,
  formatMessageClock,
  imageOcrText,
  isMessageOcrResolved,
  mergeOcrRecords,
  messageBodyText,
  nextOcrBatch,
  nextReplyTargetId,
  normalizeContext,
  normalizeContextMessage,
  normalizeEqResult,
  normalizeOcrResults,
  normalizeRecentChats,
  normalizeUnixSeconds,
  ocrEngineAvailable,
  ocrLookupKey,
  ocrSourceLabel,
  replyDrafts,
  shouldPoll,
  sortEqStyles,
  sortRecentChats,
  chatTypeLabel,
  type OcrLookup,
  type WecomContextMessage,
  type WecomRecentChat,
} from "./highEqReply.logic";

/* ------------------------------------------------------------------ */
/* fixtures                                                            */
/* ------------------------------------------------------------------ */

function chat(overrides: Partial<WecomRecentChat> = {}): WecomRecentChat {
  return {
    conversationId: "c1",
    displayName: "测试群",
    lastMessageTime: 1_700_000_000,
    isGroup: true,
    ...overrides,
  };
}

function message(overrides: Partial<WecomContextMessage> = {}): WecomContextMessage {
  return {
    messageId: "m1",
    senderId: "u1",
    senderName: "张三",
    sendTime: 1_700_000_000,
    contentType: "text",
    text: "你好",
    images: [],
    imageCount: 0,
    ...overrides,
  };
}

/** Local wall-clock seconds so `HH:mm` assertions are timezone independent. */
function localSeconds(
  year: number,
  month: number,
  day: number,
  hour: number,
  minute: number,
): number {
  return Math.floor(new Date(year, month - 1, day, hour, minute, 0).getTime() / 1000);
}

function imageMessage(overrides: Partial<WecomContextMessage> = {}): WecomContextMessage {
  return message({
    messageId: "m-img",
    contentType: "image",
    text: "[图片]",
    images: [{ index: 0, available: true, mimeType: "image/png", errorCode: "" }],
    imageCount: 1,
    ...overrides,
  });
}

function withOcrText(target: WecomContextMessage, text: string): OcrLookup {
  return {
    [ocrLookupKey(target.messageId, 0)]: {
      messageId: target.messageId,
      index: 0,
      text,
      source: "ocr",
      model: "qwen-vl-max",
      error: "",
    },
  };
}

/* ------------------------------------------------------------------ */
/* recent chats                                                        */
/* ------------------------------------------------------------------ */

describe("recent chats", () => {
  it("normalizes, de-duplicates and sorts chats newest first", () => {
    const chats = normalizeRecentChats({
      chats: [
        { conversationId: "a", displayName: "李总", lastMessageTime: 100, isGroup: false },
        { conversationId: "b", lastMessageTime: 300, isGroup: true },
        { conversationId: "c", displayName: "   ", lastMessageTime: 200, isGroup: false },
        { conversationId: "a", displayName: "重复项", lastMessageTime: 999, isGroup: true },
        { conversationId: "", displayName: "没有 ID", lastMessageTime: 999 },
      ],
    });
    expect(chats.map((item) => item.conversationId)).toEqual(["b", "c", "a"]);
    expect(chats[0].displayName).toBe("b");
    expect(chats[1].displayName).toBe("c");
    expect(chats[2].displayName).toBe("李总");
    expect(chats[2].isGroup).toBe(false);
  });

  it("accepts a bare array and tolerates missing timestamps", () => {
    const chats = normalizeRecentChats([
      { conversationId: "old", lastMessageTime: 10, isGroup: false },
      { conversationId: "unknown", isGroup: false },
    ]);
    expect(chats.map((item) => item.conversationId)).toEqual(["old", "unknown"]);
    expect(chats[1].lastMessageTime).toBe(0);
  });

  it("does not mutate the input order when sorting", () => {
    const input = [
      chat({ conversationId: "older", lastMessageTime: 10 }),
      chat({ conversationId: "newer", lastMessageTime: 20 }),
    ];
    const sorted = sortRecentChats(input);
    expect(sorted.map((item) => item.conversationId)).toEqual(["newer", "older"]);
    expect(input.map((item) => item.conversationId)).toEqual(["older", "newer"]);
  });

  it("selects the most recent chat by default", () => {
    expect(defaultChatId(normalizeRecentChats({
      chats: [
        { conversationId: "a", lastMessageTime: 10, isGroup: false },
        { conversationId: "b", lastMessageTime: 30, isGroup: true },
      ],
    }))).toBe("b");
    expect(defaultChatId([])).toBe("");
  });

  it("normalizes seconds and milliseconds", () => {
    expect(normalizeUnixSeconds(1_700_000_000)).toBe(1_700_000_000);
    expect(normalizeUnixSeconds(1_700_000_000_000)).toBe(1_700_000_000);
    expect(normalizeUnixSeconds("1700000000")).toBe(1_700_000_000);
    expect(normalizeUnixSeconds(0)).toBe(0);
    expect(normalizeUnixSeconds(Number.NaN)).toBe(0);
    expect(normalizeUnixSeconds(undefined)).toBe(0);
  });

  it("has a fingerprint that changes only when the list changes", () => {
    const first = [chat({ conversationId: "a" }), chat({ conversationId: "b", isGroup: false })];
    expect(chatsFingerprint(first)).toBe(chatsFingerprint(first.map((item) => ({ ...item }))));
    expect(chatsFingerprint(first)).not.toBe(chatsFingerprint([chat({ conversationId: "a" })]));
    expect(chatsFingerprint(first)).not.toBe(chatsFingerprint([
      chat({ conversationId: "a" }),
      chat({ conversationId: "b", isGroup: true }),
    ]));
  });
});

/* ------------------------------------------------------------------ */
/* time formatting                                                     */
/* ------------------------------------------------------------------ */

describe("time formatting", () => {
  it("formats message clocks as HH:mm", () => {
    expect(formatMessageClock(localSeconds(2026, 5, 20, 9, 5))).toBe("09:05");
    expect(formatMessageClock(0)).toBe("--:--");
    expect(formatMessageClock(-1)).toBe("--:--");
  });

  it("formats rail timestamps by day distance", () => {
    const now = new Date(2026, 4, 20, 15, 30);
    expect(formatChatTime(localSeconds(2026, 5, 20, 9, 5), now)).toBe("09:05");
    expect(formatChatTime(localSeconds(2026, 5, 19, 22, 0), now)).toBe("昨天");
    expect(formatChatTime(localSeconds(2026, 1, 5, 9, 0), now)).toBe("1月5日");
    expect(formatChatTime(localSeconds(2025, 12, 31, 9, 0), now)).toBe("2025年12月31日");
    expect(formatChatTime(0, now)).toBe("");
  });

  it("labels chat types", () => {
    expect(chatTypeLabel(true)).toBe("群聊");
    expect(chatTypeLabel(false)).toBe("私聊");
  });
});

/* ------------------------------------------------------------------ */
/* context normalization                                               */
/* ------------------------------------------------------------------ */

describe("context normalization", () => {
  it("sorts messages oldest first and normalizes fields", () => {
    const context = normalizeContext({
      conversationId: "R:1",
      displayName: "OA 群",
      isGroup: true,
      messages: [
        {
          messageId: "m2",
          senderId: "u2",
          senderName: "",
          sendTime: 300,
          contentType: "image",
          text: "  ",
          images: [
            { index: 1, available: false, mimeType: "", errorCode: "not_cached" },
            { index: 0, available: true, mimeType: "image/png", errorCode: "" },
            { index: 0, available: true, mimeType: "image/png", errorCode: "" },
          ],
        },
        {
          messageId: "m1",
          senderName: " 张三 ",
          sendTime: 100,
          contentType: "",
          text: " 你好 ",
        },
      ],
    });
    expect(context.displayName).toBe("OA 群");
    expect(context.isGroup).toBe(true);
    expect(context.messages.map((item) => item.messageId)).toEqual(["m1", "m2"]);
    expect(context.messages[0].senderName).toBe("张三");
    expect(context.messages[0].contentType).toBe("text");
    expect(context.messages[0].text).toBe("你好");
    expect(context.messages[1].senderName).toBe("未知成员");
    expect(context.messages[1].images.map((image) => image.index)).toEqual([0, 1]);
    expect(context.messages[1].imageCount).toBe(2);
  });

  it("prefers the reported imageCount but never loses descriptors", () => {
    const messageWithCount = normalizeContextMessage({
      messageId: "m1",
      imageCount: 3,
      images: [{ index: 0, available: true }],
    });
    expect(messageWithCount?.imageCount).toBe(3);
    const messageWithoutCount = normalizeContextMessage({
      messageId: "m1",
      images: [
        { index: 0, available: true },
        { index: 1, available: true },
      ],
    });
    expect(messageWithoutCount?.imageCount).toBe(2);
  });

  it("drops messages without an id", () => {
    expect(normalizeContextMessage(null)).toBeNull();
    expect(normalizeContextMessage({ messageId: "  " })).toBeNull();
    expect(normalizeContext({ messages: [null, { messageId: "" }] }).messages).toEqual([]);
  });

  it("falls back to the conversation id when no display name exists", () => {
    const context = normalizeContext({ conversationId: "R:9", messages: [] });
    expect(context.displayName).toBe("R:9");
    expect(context.isGroup).toBe(false);
  });

  it("has a fingerprint that ignores object identity but notices content changes", () => {
    const first = [message({ messageId: "m1" }), imageMessage({ messageId: "m2" })];
    const copy = first.map((item) => ({ ...item, images: item.images.map((image) => ({ ...image })) }));
    expect(contextFingerprint(first)).toBe(contextFingerprint(copy));
    expect(contextFingerprint(first)).not.toBe(contextFingerprint([
      message({ messageId: "m1", text: "改过了" }),
      imageMessage({ messageId: "m2" }),
    ]));
    expect(contextFingerprint(first)).not.toBe(contextFingerprint([message({ messageId: "m1" })]));
  });
});

/* ------------------------------------------------------------------ */
/* reply target                                                        */
/* ------------------------------------------------------------------ */

describe("reply target", () => {
  const messages = [message({ messageId: "m1" }), message({ messageId: "m2" }), message({ messageId: "m3" })];

  it("defaults to the last message", () => {
    expect(nextReplyTargetId(messages, "")).toBe("m3");
  });

  it("skips blank trailing messages when choosing a default target", () => {
    const withBlank = [
      ...messages,
      message({ messageId: "m4", text: "   " }),
    ];
    expect(nextReplyTargetId(withBlank, "")).toBe("m3");
  });
  it("keeps the user's target while it is still in the window", () => {
    expect(nextReplyTargetId(messages, "m2")).toBe("m2");
  });

  it("falls back to the last message when the target disappeared", () => {
    expect(nextReplyTargetId(messages, "m-gone")).toBe("m3");
    expect(nextReplyTargetId([], "m2")).toBe("");
  });
});

/* ------------------------------------------------------------------ */
/* polling                                                             */
/* ------------------------------------------------------------------ */

describe("polling policy", () => {
  it("only polls for active pages in visible windows", () => {
    expect(shouldPoll(true, "visible")).toBe(true);
    expect(shouldPoll(undefined, "visible")).toBe(true);
    expect(shouldPoll(true, "hidden")).toBe(false);
    expect(shouldPoll(false, "visible")).toBe(false);
    expect(shouldPoll(true, undefined)).toBe(false);
  });

  it("keeps the 30 second cadence", () => {
    expect(POLL_INTERVAL_MS).toBe(30_000);
    expect(CONTEXT_LIMIT).toBe(30);
    expect(CONTEXT_WINDOW_SECONDS).toBe(21_600);
    expect(OCR_BATCH_LIMIT).toBe(6);
  });
});

/* ------------------------------------------------------------------ */
/* OCR                                                                 */
/* ------------------------------------------------------------------ */

describe("ocr merging", () => {
  it("keys results by messageId and index and keeps older rows", () => {
    const current: OcrLookup = withOcrText(imageMessage({ messageId: "m-old" }), "旧结果");
    const merged = mergeOcrRecords(current, {
      results: [
        { messageId: "m-img", index: 0, text: " 截图文字 ", source: "ocr", model: "m1", error: "" },
        { messageId: "m-img", index: 1, text: "", source: "llm", model: "m2", error: "图片不可用" },
      ],
      engine: { available: true },
    });
    expect(merged[ocrLookupKey("m-img", 0)].text).toBe("截图文字");
    expect(merged[ocrLookupKey("m-img", 0)].source).toBe("ocr");
    expect(merged[ocrLookupKey("m-img", 1)].error).toBe("图片不可用");
    expect(merged[ocrLookupKey("m-old", 0)].text).toBe("旧结果");
  });

  it("later batches overwrite the same image result", () => {
    const first = mergeOcrRecords({}, {
      results: [{ messageId: "m1", index: 0, text: "旧", source: "ocr", error: "" }],
    });
    const second = mergeOcrRecords(first, {
      results: [{ messageId: "m1", index: 0, text: "新", source: "llm", error: "" }],
    });
    expect(second[ocrLookupKey("m1", 0)].text).toBe("新");
    expect(second[ocrLookupKey("m1", 0)].source).toBe("llm");
  });

  it("ignores malformed OCR rows", () => {
    expect(normalizeOcrResults({
      results: [
        null,
        { index: 0, text: "没有消息 ID" },
        { messageId: "m1", index: -1, text: "负数序号" },
        { messageId: "m1", index: 2, text: "ok", source: "unknown" },
      ],
    })).toEqual([
      { messageId: "m1", index: 2, text: "ok", source: "ocr", model: "", error: "" },
    ]);
  });

  it("reports whether the OCR engine is configured", () => {
    expect(ocrEngineAvailable({ engine: { available: true } })).toBe(true);
    expect(ocrEngineAvailable({ engine: { available: false } })).toBe(false);
    expect(ocrEngineAvailable({})).toBe(false);
    expect(ocrEngineAvailable(null)).toBe(false);
  });

  it("maps OCR text and failures to display copy", () => {
    expect(imageOcrText(undefined)).toBe(OCR_EMPTY_PLACEHOLDER);
    expect(imageOcrText({
      messageId: "m1", index: 0, text: "识别出的文字", source: "ocr", model: "", error: "",
    })).toBe("识别出的文字");
    expect(imageOcrText({
      messageId: "m1", index: 0, text: "", source: "ocr", model: "", error: "图片 OCR 超时",
    })).toBe(OCR_TIMEOUT_PLACEHOLDER);
    expect(imageOcrText({
      messageId: "m1", index: 0, text: "", source: "ocr", model: "", error: "未配置 OCR 模型",
    })).toBe(OCR_EMPTY_PLACEHOLDER);
    expect(imageOcrText({
      messageId: "m1", index: 0, text: "", source: "ocr", model: "", error: "图片不可用",
    })).toBe(OCR_EMPTY_PLACEHOLDER);
    expect(imageOcrText({
      messageId: "m1", index: 0, text: "", source: "ocr", model: "", error: "识别服务返回错误",
    })).toBe(OCR_FAILED_PLACEHOLDER);
  });

  it("labels the OCR source", () => {
    expect(ocrSourceLabel(undefined)).toBe("图片 OCR 识别");
    expect(ocrSourceLabel({
      messageId: "m1", index: 0, text: "x", source: "llm", model: "", error: "",
    })).toBe("全局视觉模型识别");
  });
});

describe("ocr batching", () => {
  it("only asks for image messages that do not have results yet", () => {
    const textOnly = message({ messageId: "m-text" });
    const pending = imageMessage({ messageId: "m-pending" });
    const resolved = imageMessage({ messageId: "m-resolved" });
    const lookup: OcrLookup = withOcrText(resolved, "已有文字");
    expect(nextOcrBatch([textOnly, pending, resolved], lookup)).toEqual(["m-pending"]);
  });

  it("treats error results as resolved and counts imageCount even without descriptors", () => {
    const failed = imageMessage({ messageId: "m-failed" });
    const lookup: OcrLookup = {
      [ocrLookupKey("m-failed", 0)]: {
        messageId: "m-failed", index: 0, text: "", source: "ocr", model: "", error: "图片 OCR 超时",
      },
    };
    expect(isMessageOcrResolved(failed, lookup)).toBe(true);
    const noDescriptor = message({ messageId: "m-count", imageCount: 1 });
    expect(isMessageOcrResolved(noDescriptor, {})).toBe(false);
    expect(nextOcrBatch([failed, noDescriptor], lookup, {})).toEqual(["m-count"]);
  });

  it("caps a batch at six message ids and continues serially", () => {
    const messages = Array.from({ length: 8 }, (_, index) =>
      imageMessage({ messageId: `m${index}` }));
    const first = nextOcrBatch(messages, {}, {});
    expect(first).toHaveLength(6);
    expect(first).toEqual(["m0", "m1", "m2", "m3", "m4", "m5"]);
    // One serial round merges a record (text or error) for every requested image.
    const lookup: OcrLookup = {};
    for (const id of first) {
      lookup[ocrLookupKey(id, 0)] = {
        messageId: id, index: 0, text: "", source: "ocr", model: "", error: "未配置 OCR 模型",
      };
    }
    const attempts = Object.fromEntries(first.map((id) => [id, 1]));
    const second = nextOcrBatch(messages, lookup, attempts);
    expect(second).toEqual(["m6", "m7"]);
  });

  it("stops retrying after the attempt budget", () => {
    const stuck = imageMessage({ messageId: "m-stuck" });
    expect(nextOcrBatch([stuck], {}, { "m-stuck": OCR_MAX_MESSAGE_ATTEMPTS })).toEqual([]);
    expect(nextOcrBatch([stuck], {}, { "m-stuck": 1 })).toEqual(["m-stuck"]);
  });
});

/* ------------------------------------------------------------------ */
/* message body + context composition                                  */
/* ------------------------------------------------------------------ */

describe("context composition", () => {
  it("renders text messages as `[HH:mm] sender: content`", () => {
    const target = message({
      messageId: "m1",
      senderName: "张三",
      sendTime: localSeconds(2026, 5, 20, 9, 5),
      text: "你好",
    });
    expect(composeContextText([target], {})).toBe("[09:05] 张三: 你好");
    expect(composeContextText([], {})).toBe("");
  });

  it("includes OCR text for image messages", () => {
    const image = imageMessage({
      messageId: "m-img",
      senderName: "李四",
      sendTime: localSeconds(2026, 5, 20, 9, 6),
    });
    const text = composeContextText([image], withOcrText(image, "截图：订单导出失败"));
    expect(text).toBe("[09:06] 李四: 截图：订单导出失败");
  });

  it("keeps one line per message and can exclude the reply target", () => {
    const first = message({ messageId: "m1", sendTime: 100, text: "第一行\n第二行" });
    const target = message({ messageId: "m2", sendTime: 200, text: "要回复的消息" });
    const composed = composeContextText([first, target], {}, { excludeMessageId: "m2" });
    expect(composed.split("\n")).toHaveLength(1);
    expect(composed).toContain("第一行 第二行");
    expect(composed).not.toContain("要回复的消息");
  });

  it("composes message bodies from text, OCR text and placeholders", () => {
    const image = imageMessage({ messageId: "m-img" });
    expect(messageBodyText(image, {})).toBe(OCR_EMPTY_PLACEHOLDER);
    expect(messageBodyText(image, withOcrText(image, "识别文字"))).toBe("识别文字");
    const withCaption = imageMessage({ messageId: "m-img2", text: "[图片]" });
    expect(messageBodyText(withCaption, withOcrText(withCaption, "OCR"))).toBe("OCR");
    expect(messageBodyText(message({ text: "   " }), {})).toBe("[空消息]");
    expect(messageBodyText(message({ text: "", contentType: "voice" }), {})).toBe("[语音]");
  });
});

/* ------------------------------------------------------------------ */
/* eq_reply normalization                                              */
/* ------------------------------------------------------------------ */

describe("eq_reply normalization", () => {
  it("orders the three styles and fills labels", () => {
    const result = normalizeEqResult({
      styles: [
        { id: "brief", title: "", content: "简短版" },
        { id: "steady", content: "稳重版" },
        { id: "warm", content: "亲和版" },
        { id: "steady", content: "重复的稳重版" },
          { id: "steady", content: "" },
      ],
      interpretation: {
        summary: "对方在催进度",
        signals: [
          { label: "时间压力", detail: "要求今天给说法" },
          { label: "", detail: "只有细节" },
          { label: "", detail: "" },
        ],
      },
      check: {
        risks: "只给“尽快”会显得没有掌控感\n- 承诺时间可能再次延期",
        suggestions: ["给出具体时间点", "说明延期会提前同步"],
      },
      fallback: true,
    });
    expect(result.styles.map((style) => style.id)).toEqual(["steady", "warm", "brief"]);
    expect(result.styles.map((style) => style.title)).toEqual(["稳重", "亲和", "简短"]);
    expect(result.styles[0].content).toBe("稳重版");
    expect(result.interpretation.summary).toBe("对方在催进度");
    expect(result.interpretation.signals).toEqual([
      { label: "时间压力", detail: "要求今天给说法" },
      { label: "信号", detail: "只有细节" },
    ]);
    expect(result.check.risks).toEqual(["只给“尽快”会显得没有掌控感", "承诺时间可能再次延期"]);
    expect(result.check.suggestions).toEqual(["给出具体时间点", "说明延期会提前同步"]);
    expect(result.fallback).toBe(true);
  });

  it("survives an empty or malformed response", () => {
    const result = normalizeEqResult(null);
    expect(result.styles).toEqual([]);
    expect(result.interpretation).toEqual({ summary: "", signals: [] });
    expect(result.check).toEqual({ risks: [], suggestions: [] });
    expect(result.fallback).toBe(false);
    expect(replyDrafts(result)).toEqual({});
  });

  it("builds editable drafts from the styles", () => {
    const result = normalizeEqResult({
      styles: [
        { id: "steady", content: "A" },
        { id: "warm", content: "B" },
        { id: "brief", content: "C" },
      ],
    });
    expect(replyDrafts(result)).toEqual({ steady: "A", warm: "B", brief: "C" });
  });

  it("sorts unknown style ids after the known ones", () => {
    expect(sortEqStyles([
      { id: "custom" },
      { id: "brief" },
      { id: "steady" },
    ]).map((style) => style.id)).toEqual(["steady", "brief", "custom"]);
    expect(eqStyleLabel("steady")).toBe("稳重");
    expect(eqStyleLabel("custom")).toBe("custom");
  });

  it("trims the action payload and omits empty extras", () => {
    expect(eqReplyPayload({ text: "  在吗  " })).toEqual({ text: "在吗" });
    expect(eqReplyPayload({
      text: "在吗",
      context: " [09:00] 张三: 你好 ",
      recipient: " 张总 ",
    })).toEqual({ text: "在吗", context: "[09:00] 张三: 你好", recipient: "张总" });
    expect(eqReplyPayload({ text: "在吗", context: "   ", recipient: "" })).toEqual({ text: "在吗" });
  });
});

describe("display hardening", () => {
  it("hides raw direct-chat ids behind a readable label", () => {
    expect(chatDisplayName({ conversationId: "S:1_2", displayName: "S:1_2", isGroup: false })).toBe("\u79c1\u804a\u4f1a\u8bdd");
    expect(chatDisplayName({ conversationId: "S:1_2", displayName: "\u674e\u603b", isGroup: false })).toBe("\u674e\u603b");
    expect(chatDisplayName({ conversationId: "R:1", displayName: "", isGroup: true })).toBe("\u7fa4\u804a");
  });

  it("falls back for numeric sender ids", () => {
    expect(senderDisplayName("337769972893279308", "337769972893279308")).toBe("\u672a\u77e5\u6210\u5458");
    expect(senderDisplayName("\u738b\u5c0f\u660e", "1")).toBe("\u738b\u5c0f\u660e");
  });

  it("summarizes app-card JSON instead of showing raw payloads", () => {
    const text = sanitizeMessageText(
      '{"source_icon_desc":"","url":"","desc":"\u4f01\u5fae\u52a9\u624b","display":{"title":"\u667a\u80fd\u81ea\u52a8\u5316\u52a9\u624b","content":"\u6c99\u7bb1\u5185\u4e0d\u5b58\u5728 /data"}}',
    );
    expect(text).toContain("\u4f01\u5fae\u52a9\u624b");
    expect(text).toContain("\u667a\u80fd\u81ea\u52a8\u5316\u52a9\u624b");
    expect(text).not.toContain("source_icon_desc");
  });

  it("strips control and zero-width characters", () => {
    expect(sanitizeMessageText("\u200b\u0000hello\n\n\nworld")).toBe("hello\n\nworld");
  });

  it("labels non-text content types", () => {
    expect(contentTypeLabel("app")).toBe("\u5e94\u7528\u6d88\u606f");
    expect(contentTypeLabel("unsupported:40")).toBe("\u975e\u6587\u672c\u6d88\u606f");
  });

  it("returns a readable placeholder when a message has no body", () => {
    const message = {
      messageId: "1",
      senderId: "1",
      senderName: "\u672a\u77e5\u6210\u5458",
      sendTime: 0,
      contentType: "app",
      text: "",
      images: [],
      imageCount: 0,
    };
    expect(messageDisplayText(message)).toBe("[\u5e94\u7528\u6d88\u606f]");
  });
});

describe("message folding and blank text", () => {
  it("collapses long or multi-line messages", () => {
    expect(shouldCollapseMessage("short")).toBe(false);
    expect(shouldCollapseMessage("x".repeat(200))).toBe(true);
    expect(shouldCollapseMessage("a\nb\nc\nd")).toBe(true);
  });

  it("never renders image download urls as text", () => {
    const message = imageMessage({
      messageId: "m-url",
      text: "https://immunion.weixin.qq.com/cgi-bin/mmbae-bin/t/downloadmedia?abc",
    });
    expect(messageDisplayText(message)).toBe("");
    expect(messageBodyText(message, {})).toBe(OCR_EMPTY_PLACEHOLDER);
  });

  it("replaces whitespace-only payloads with a placeholder", () => {
    expect(messageDisplayText(message({ text: "   \n\t  " }))).toBe("[\u7a7a\u6d88\u606f]");
  });

  it("carries the quiet-chat fallback flag", () => {
    const context = normalizeContext({
      conversationId: "S:1",
      displayName: "x",
      messages: [],
      windowFallback: true,
    });
    expect(context.windowFallback).toBe(true);
  });
});

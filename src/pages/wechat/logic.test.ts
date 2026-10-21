import { describe, expect, it } from "vitest";

import {
  canLoadMore,
  draftIsStale,
  emptyDraft,
  formatClock,
  formatRelative,
  incrementUnread,
  mergeMessages,
  mergeProjectMessages,
  nextMessageLimit,
  normalizeChats,
  normalizeDetect,
  normalizeDraft,
  normalizeExportResult,
  normalizeMediaPreview,
  normalizeMessages,
  normalizePoll,
  normalizeProjects,
  normalizeProject,
  normalizeProviders,
  normalizeScanResult,
  normalizeSettings,
  normalizeTimestamp,
  providerLabel,
  unreadForProject,
} from "./logic";
import type { WechatMessage, WechatProject } from "./types";

function message(overrides: Partial<WechatMessage>): WechatMessage {
  return {
    id: "m1",
    projectId: "p1",
    sourceDb: "db",
    localId: 1,
    sortSeq: 1,
    createTime: 1_700_000_000,
    senderId: "wxid",
    senderName: "张三",
    direction: "incoming",
    kind: "text",
    text: "你好",
    quote: null,
    media: [],
    mentionedMe: false,
    ...overrides,
  };
}

function project(overrides: Partial<WechatProject> = {}): WechatProject {
  return {
    id: "p1",
    accountId: "acc",
    chatId: "wxid_a",
    chatTitle: "张三",
    name: "张三项目",
    kind: "direct",
    enabled: true,
    summary: "",
    tone: "自然友好",
    prompt: "",
    trigger: { mode: "auto", keywords: [], mentionMe: false, quietWindowMs: 1800 },
    providerId: "",
    skillPaths: [],
    mcpServerIds: [],
    revision: 0,
    createdAt: "",
    updatedAt: "",
    ...overrides,
  };
}

describe("timestamp and formatting helpers", () => {
  it("normalizes seconds, milliseconds and ISO strings", () => {
    expect(normalizeTimestamp(1_700_000_000)).toBe(1_700_000_000);
    expect(normalizeTimestamp(1_700_000_000_000)).toBe(1_700_000_000);
    expect(normalizeTimestamp("1700000000")).toBe(1_700_000_000);
    expect(normalizeTimestamp("2026-07-20T10:00:00Z")).toBe(
      Math.floor(Date.parse("2026-07-20T10:00:00Z") / 1000),
    );
    expect(normalizeTimestamp(undefined)).toBe(0);
  });

  it("formats clock values and relative times", () => {
    expect(formatClock(1_700_000_000)).toMatch(/\d{1,2}:\d{2}/);
    const now = Date.parse("2026-07-20T12:00:00Z");
    expect(formatRelative(Math.floor(now / 1000) - 30, now)).toBe("刚刚");
    expect(formatRelative(Math.floor(now / 1000) - 600, now)).toBe("10 分钟前");
    expect(formatRelative(Math.floor(now / 1000) - 7200, now)).toBe("2 小时前");
    expect(formatRelative(0)).toBe("");
  });
});

describe("detect and scan normalizers", () => {
  it("accepts snake_case and string account lists", () => {
    const result = normalizeDetect({
      found: true,
      db_dir: "C:/wechat/db",
      running: true,
      version: "4.0.3",
      accounts: ["wxid_1"],
    });
    expect(result).toMatchObject({
      found: true,
      dbDir: "C:/wechat/db",
      running: true,
      version: "4.0.3",
    });
    expect(result.accounts[0]).toMatchObject({ accountId: "wxid_1" });
  });

  it("infers detection from a configured directory", () => {
    expect(normalizeDetect({ configured_db_dir: "D:/db" }).found).toBe(true);
    expect(normalizeDetect({}).found).toBe(false);
  });

  it("normalizes scan results", () => {
    expect(normalizeScanResult({ success: true, detail: "5 个数据库", account_id: "wxid" })).toEqual({
      ok: true,
      message: "5 个数据库",
      accountId: "wxid",
    });
    expect(normalizeScanResult({}).ok).toBe(false);
  });
});

describe("chat and project normalizers", () => {
  it("detects group chats from kind or @chatroom ids", () => {
    const chats = normalizeChats({
      chats: [
        { chat_id: "123@chatroom", name: "产品群", unread: "3", last_timestamp: 1_700_000_000 },
        { chatId: "wxid_b", displayName: "李四", kind: "direct" },
      ],
    });
    expect(chats).toHaveLength(2);
    expect(chats[0]).toMatchObject({ kind: "group", unread: 3, displayName: "产品群" });
    expect(chats[1]).toMatchObject({ kind: "direct", displayName: "李四" });
  });

  it("preserves unknown project fields while normalizing known ones", () => {
    const [normalized] = normalizeProjects({
      projects: [{
        id: "p1",
        project_id: "p1",
        chat_id: "wxid_a",
        chat_title: "张三",
        name: "张三项目",
        kind: "direct",
        summary: "上下文",
        tone_preset: "专业稳重",
        trigger: { mode: "rules", keywords: ["报价", " 排期 "], mention_me: true },
        skill_paths: ["C:/skill"],
        custom_backend_field: 42,
      }],
    });
    expect(normalized).toMatchObject({
      id: "p1",
      chatId: "wxid_a",
      summary: "上下文",
      tone: "专业稳重",
      skillPaths: ["C:/skill"],
      custom_backend_field: 42,
    });
    expect(normalized.trigger).toEqual({
      mode: "rules",
      keywords: ["报价", "排期"],
      mentionMe: true,
      quietWindowMs: 1800,
    });
  });

  it("defaults group triggers to manual and direct triggers to auto", () => {
    const [group] = normalizeProjects({ projects: [{ id: "g", kind: "group" }] });
    const [direct] = normalizeProjects({ projects: [{ id: "d", kind: "direct" }] });
    expect(group.trigger.mode).toBe("manual");
    expect(direct.trigger.mode).toBe("auto");
  });

  it("unwraps a saved project envelope returned by project_save", () => {
    const saved = normalizeProject({
      project: { id: "p2", chat_id: "c2", name: "新项目", revision: 3 },
      draft: { content: "草稿" },
    });
    expect(saved).toMatchObject({ id: "p2", chatId: "c2", name: "新项目", revision: 3 });
  });

  it("accepts a poll entry that nests the project object", () => {
    const entries = normalizePoll({
      projects: [{
        project: { id: "p1" },
        newMessages: [{ id: "n1", direction: "incoming", text: "hi" }],
      }],
    });
    expect(entries[0]).toMatchObject({ projectId: "p1", unreadDelta: 1 });
  });
});

describe("message normalizers and merges", () => {
  it("normalizes snake_case messages, quotes and media", () => {
    const messages = normalizeMessages({
      messages: [{
        id: "m2",
        project_id: "p1",
        create_time: 1_700_000_100,
        sender_name: "李四",
        direction: "out",
        kind: "image",
        content: "[图片]",
        quote: { sender: "张三", text: "原文" },
        media: [{ path: "a.jpg" }],
        mentioned_me: true,
      }],
    }, "p1");
    expect(messages[0]).toMatchObject({
      id: "m2",
      projectId: "p1",
      direction: "outgoing",
      kind: "image",
      senderName: "李四",
      text: "[图片]",
      mentionedMe: true,
    });
    expect(messages[0].quote).toEqual({ sender: "张三", text: "原文" });
    expect(messages[0].media).toHaveLength(1);
  });

  it("sorts by create time and deduplicates by id", () => {
    const merged = mergeMessages(
      [message({ id: "b", createTime: 20 })],
      [
        message({ id: "a", createTime: 10 }),
        message({ id: "b", createTime: 30, text: "更新" }),
      ],
    );
    expect(merged.map((item) => item.id)).toEqual(["a", "b"]);
    expect(merged[1]?.text).toBe("更新");
    expect(mergeProjectMessages({ p1: [message({ id: "x" })] }, "p1", [message({ id: "y" })])).toMatchObject({
      added: 1,
    });
  });

  it("keeps message ids stable for rows without an id", () => {
    const messages = normalizeMessages([
      { sort_seq: 1, local_id: 1, create_time: 100, text: "a" },
      { sort_seq: 1, local_id: 1, create_time: 100, text: "b" },
    ], "p1");
    expect(messages).toHaveLength(2);
    expect(new Set(messages.map((item) => item.id)).size).toBe(2);
  });
});

describe("draft and poll normalizers", () => {
  it("normalizes a draft envelope and stale flag", () => {
    const draft = normalizeDraft({
      draft: { project_id: "p1", content: "草稿", stale: true, model: "gpt", updated_at: "2026-07-20T10:00:00Z" },
    });
    expect(draft).toMatchObject({
      projectId: "p1",
      content: "草稿",
      stale: true,
      model: "gpt",
      status: "ready",
    });
    expect(normalizeDraft(null)).toBeNull();
  });

  it("merges poll entries with messages, draft and unread deltas", () => {
    const entries = normalizePoll({
      projects: [{
        project_id: "p1",
        newMessages: [{ id: "n1", direction: "incoming", text: "新消息", create_time: 1_700_000_200 }],
        draft: { project_id: "p1", content: "更新后的草稿" },
      }],
    });
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ projectId: "p1", unreadDelta: 1 });
    expect(entries[0].newMessages[0]?.text).toBe("新消息");
    expect(entries[0].draft?.content).toBe("更新后的草稿");
  });

  it("accepts numeric new-message counts without a message array", () => {
    const entries = normalizePoll([{ projectId: "p1", newMessageCount: 4 }]);
    expect(entries[0]).toMatchObject({ projectId: "p1", unreadDelta: 4, newMessages: [] });
  });

  it("flags stale drafts only when newer messages arrived", () => {
    const base = { ...emptyDraft("p1"), content: "回复", updatedAt: "2026-07-20T10:00:00Z" };
    expect(draftIsStale(base, [])).toBe(false);
    expect(draftIsStale(base, [message({ createTime: Math.floor(Date.parse("2026-07-20T10:05:00Z") / 1000) })])).toBe(true);
    expect(draftIsStale({ ...base, stale: true }, [])).toBe(true);
    expect(draftIsStale(emptyDraft("p1"), [])).toBe(false);
  });
});

describe("misc normalizers and helpers", () => {
  it("reads providers and nested MCP servers", () => {
    const snapshot = normalizeProviders({
      providers: [{ id: "p", name: "OpenAI", model: "gpt", has_secret: true }],
      mcp_servers: [{ id: "s", name: "KB", tools: [{ name: "search" }] }],
    });
    expect(snapshot.providers[0]).toMatchObject({ id: "p", model: "gpt", hasSecret: true });
    expect(snapshot.mcpServers[0]).toMatchObject({ id: "s", name: "KB", toolCount: 1 });
    expect(providerLabel(snapshot.providers, "p")).toBe("OpenAI · gpt");
    expect(providerLabel(snapshot.providers, "missing")).toBe("未选择模型");
  });

  it("applies defaults to settings", () => {
    expect(normalizeSettings({})).toMatchObject({
      pollIntervalSeconds: 5,
      contextMessageLimit: 50,
    });
    expect(normalizeSettings({ settings: { poll_interval_seconds: 3, context_message_limit: 80 } })).toMatchObject({
      pollIntervalSeconds: 3,
      contextMessageLimit: 80,
    });
  });

  it("normalizes export files and media previews", () => {
    expect(normalizeExportResult({ path: "D:/out", files: ["chat.md", { path: "chat.json" }] })).toEqual({
      dir: "D:/out",
      files: ["chat.md", "chat.json"],
    });
    expect(normalizeMediaPreview({ dataUrl: "data:image/png;base64,AAA" })).toEqual({
      dataUrl: "data:image/png;base64,AAA",
      mime: "image/png",
    });
    expect(() => normalizeMediaPreview({ reason: "文件缺失" })).toThrow("文件缺失");
  });

  it("caps paging and increments unread counters", () => {
    expect(nextMessageLimit(50)).toBe(100);
    expect(nextMessageLimit(500)).toBe(500);
    expect(canLoadMore(new Array(50).fill(message({})), 50)).toBe(true);
    expect(canLoadMore(new Array(10).fill(message({})), 50)).toBe(false);
    expect(incrementUnread({ p1: 2 }, { p1: 3, p2: 1 })).toEqual({ p1: 5, p2: 1 });
  });

  it("uses the larger of chat unread and poll unread", () => {
    const target = project();
    expect(unreadForProject(target, [{ chatId: "wxid_a" } as never], { p1: 4 })).toBe(4);
    expect(unreadForProject(target, [{ chatId: "wxid_a", unread: 7 } as never], { p1: 2 })).toBe(7);
    expect(unreadForProject(target, [], {})).toBe(0);
  });
});

/**
 * Dev-only UI preview harness.
 *
 * Launches Microsoft Edge headless, injects a mock `window.desktop` bridge and
 * captures screenshots of the real React app without needing Electron, WeCom or
 * a Python sidecar. Usage:
 *
 *   node scripts/ui-preview.mjs --out screenshots/after
 *   node scripts/ui-preview.mjs --out screenshots/after --only workbench,settings
 */
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const EDGE =
  process.env.EDGE_PATH ||
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const APP_URL = process.env.PREVIEW_URL || "http://127.0.0.1:1420";

const args = process.argv.slice(2);
const outDir = valueAfter("--out") || "screenshots/preview";
const only = (valueAfter("--only") || "").split(",").filter(Boolean);
const width = Number(valueAfter("--width") || 1280);
const height = Number(valueAfter("--height") || 820);
const dark = args.includes("--dark");
const audit = args.includes("--audit");
const port = Number(valueAfter("--port") || process.env.CDP_PORT || 9222);
const emptyListeners = process.env.PREVIEW_EMPTY_LISTENERS === "1";
const pageUrl = dark ? `${APP_URL}${APP_URL.includes("?") ? "&" : "?"}dark=1` : APP_URL;

function valueAfter(flag) {
  const index = args.indexOf(flag);
  return index >= 0 ? args[index + 1] : undefined;
}

const now = Date.now();
const mockConfig = {
  config_version: 4,
  wxwork_db_dir: "E:/Documents/WXWork/1688856756296405/Data",
  wxwork_keys_file: "wxwork_keys.json",
  target_group_id: "R:10738374660982926",
  target_group_name: "OA开发对接群（九机+九讯云）",
  default_workspace: "E:/Documents/WeComIssueRadar",
  timezone: "Asia/Shanghai",
  ocr: { base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", api_key: "", model: "qwen-vl-max", concurrency: 2 },
  llm: { provider: "openai_compatible", base_url: "https://api.deepseek.com/v1", api_key: "sk-demo", model: "deepseek-v4.1-flash", temperature: 0.1 },
  jev: { enabled: false, mode: "shadow", base_url: "", api_key: "", model: "jev-latest", timeout_seconds: 30, total_budget_seconds: 30, max_state_chars: 6000, decision_threshold: 0.7, review_floor: 0.3, max_candidates: 10, max_retries: 2, retry_backoff_seconds: 0.5, max_retry_backoff_seconds: 8, thresholds: {}, features: {} },
  diagnostics: { agent_execution_logging: false },
  prompts: {
    default_id: "daily_issue_standard",
    default_issue_fields: [],
    items: [
      { id: "daily_issue_standard", name: "OA问题同步", description: "识别咨询、故障和需求，整理问题、结论与时间轴。", content: "你是一名企业软件产品经理……", issue_fields: [
        { key: "problem_description", label: "问题描述", type: "long_text", required: true, instruction: "", options: [], default_value: "" },
        { key: "module_text", label: "模块", type: "single_select", required: true, instruction: "", options: ["订单/售后", "回收", "财务"], default_value: "订单/售后" },
        { key: "issue_category_text", label: "问题分类", type: "single_select", required: false, instruction: "", options: ["功能缺陷", "需求", "待评估"], default_value: "待评估" },
      ], scene: "issue_analysis" },
      { id: "reply_draft", name: "群回复口径", description: "群监听拟稿时的口径与边界", content: "先确认事实，再给出下一步。", issue_fields: [], scene: "reply" },
      { id: "weekly_report", name: "周报模板", description: "把一周消息整理成周报", content: "按进展/问题/下周计划输出。", issue_fields: [], scene: "weekly_report" },
      { id: "eq_reply", name: "高情商回复", description: "给领导/客户消息的三版措辞", content: "给出稳重/亲和/简短三版。", issue_fields: [], scene: "eq_reply" },
      { id: "eq_reply_customer", name: "客户群 · 先共情再给方案", description: "客户催进度/投诉时的口径", content: "先接住情绪，再给明确下一步。", issue_fields: [], scene: "eq_reply" },
      { id: "eq_reply_supplier", name: "供应商群 · 稳住交期", description: "供应商沟通交期与异常", content: "先确认事实，再给可执行的节点。", issue_fields: [], scene: "eq_reply" },
    ],
  },
  ai_strategies: [],
  publish_targets: [
    { id: "excel", name: "Excel 导出", kind: "excel", config: {} },
    { id: "markdown", name: "Markdown 导出", kind: "markdown", config: {} },
    { id: "default", name: "默认问题清单", kind: "tencent_smartsheet", config: { url: "", template_id: "default", batch_size: 50 } },
  ],
  smart_sheet: {
    default_template_id: "default",
    templates: [{ id: "default", name: "默认问题清单", url: "", webhook_url_env: "", webhook_url: "", batch_size: 50, schema: {}, field_mappings: [] }],
    upload: { token_endpoint: "", image_upload_endpoint: "", image_form_field: "media", propagation_wait_ms_after_uploads: 5000, delay_ms_between_image_uploads: 400, corpid: "", corpsecret: "" },
  },
  schedules: [
    { id: "daily", name: "每日群聊导出", enabled: false, runAt: "18:30", weekdays: [1, 2, 3, 4, 5], dateMode: "yesterday", fixedDate: "", startTime: "00:00", endTime: "23:59", groups: [{ id: "1", name: "OA开发对接群" }, { id: "2", name: "泛中台对接群" }], promptId: "daily_issue_standard", smartSheetTemplateId: "default", runOcr: true, runAnalysis: true, exportXlsx: true, exportMarkdown: true, prepareSmartSheet: true },
  ],
  mcp_servers: [],
  reply_listeners: [],
};

const mockBridge = `
(() => {
  try {
    if (location.search.includes("dark=1")) localStorage.setItem("issue-radar-theme", "dark");
  } catch {}
  const now = ${now};
  const config = ${JSON.stringify(mockConfig)};
  const emptyListeners = ${emptyListeners ? "true" : "false"};
  const listeners = [
    { id: "l1", revision: 1, name: "OA开发对接群（九机+九讯云）自动答疑", enabled: false, groupId: "1", groupName: "OA开发对接群（九机+九讯云）", answerEngine: "mcp", runtimeVersion: "v2", difyAppId: "", systemPrompt: "", webhookConfigured: true, webhookVerified: true, deliveryMode: "review", requireIndependentReview: true, reviewPrompt: "", tools: [], tuning: { pollIntervalSeconds: 5, sameSenderMergeSeconds: 30, humanReplyWaitSeconds: 20, sessionTimeoutSeconds: 300, maxConcurrency: 2, mcpTimeoutSeconds: 60, difyTimeoutSeconds: 60, maxAgentRounds: 6, maxToolCalls: 20 }, health: "stopped", pendingCount: 0, toolSafety: { readOnly: 27, unknown: 0, write: 0, destructive: 0, blocked: 0, bootstrap: 4 } },
    { id: "l2", revision: 1, name: "回收良品开发对接群自动答疑", enabled: false, groupId: "2", groupName: "回收良品开发对接群", answerEngine: "mcp", runtimeVersion: "legacy", difyAppId: "", systemPrompt: "", webhookConfigured: true, webhookVerified: true, deliveryMode: "review", requireIndependentReview: true, reviewPrompt: "", tools: [], tuning: { pollIntervalSeconds: 5, sameSenderMergeSeconds: 30, humanReplyWaitSeconds: 20, sessionTimeoutSeconds: 300, maxConcurrency: 2, mcpTimeoutSeconds: 60, difyTimeoutSeconds: 60, maxAgentRounds: 6, maxToolCalls: 20 }, health: "stopped", pendingCount: 0, toolSafety: { readOnly: 27, unknown: 0, write: 0, destructive: 0, blocked: 0, bootstrap: 4 } },
    { id: "l3", revision: 1, name: "泛中台对接", enabled: false, groupId: "3", groupName: "泛中台对接", answerEngine: "mcp", runtimeVersion: "v2", difyAppId: "", systemPrompt: "", webhookConfigured: true, webhookVerified: true, deliveryMode: "review", requireIndependentReview: true, reviewPrompt: "", tools: [], tuning: { pollIntervalSeconds: 5, sameSenderMergeSeconds: 30, humanReplyWaitSeconds: 20, sessionTimeoutSeconds: 300, maxConcurrency: 2, mcpTimeoutSeconds: 60, difyTimeoutSeconds: 60, maxAgentRounds: 6, maxToolCalls: 20 }, health: "stopped", pendingCount: 0, toolSafety: { readOnly: 27, unknown: 0, write: 0, destructive: 0, blocked: 0, bootstrap: 4 } },
    { id: "l4", revision: 1, name: "财务开发组自动答疑", enabled: false, groupId: "4", groupName: "财务开发组", answerEngine: "mcp", runtimeVersion: "v2", difyAppId: "", systemPrompt: "", webhookConfigured: true, webhookVerified: true, deliveryMode: "review", requireIndependentReview: true, reviewPrompt: "", tools: [], tuning: { pollIntervalSeconds: 5, sameSenderMergeSeconds: 30, humanReplyWaitSeconds: 20, sessionTimeoutSeconds: 300, maxConcurrency: 2, mcpTimeoutSeconds: 60, difyTimeoutSeconds: 60, maxAgentRounds: 6, maxToolCalls: 20 }, health: "stopped", pendingCount: 0, toolSafety: { readOnly: 27, unknown: 0, write: 0, destructive: 0, blocked: 0, bootstrap: 4 } },
  ];
  const runtimeSnapshot = { running: true, pendingCount: 0, activeRetrievals: 0, queuedRetrievals: 0, listeners, recentFailures: 56 };
  const work = (id, status, question, answer) => ({ id: "w" + id, version: 1, listenerId: "l1", listenerName: "OA开发对接群", groupId: "1", groupName: "OA开发对接群", senderName: "王小明", question, answer, status, stage: status === "waiting" ? "human_wait" : "ready", createdAt: new Date(now - id * 60000).toISOString(), updatedAt: new Date(now - id * 60000).toISOString(), imageStatus: "none" });
  const workList = [work(1, "waiting", "客户说订单导出一直转圈，是不是系统挂了？", ""), work(2, "pending", "回收模块的称重数据对不上，能帮忙看下吗？", "已收到，我先查一下称重接口的日志。"), work(3, "waiting", "这个月对账单什么时候能出？", "")];
  const highEqCatalog = [
    { conversationId: "R:10738374660982926", displayName: "OA开发对接群（九机+九讯云）", lastMessageTime: Math.floor(now / 1000) - 120, lastMessageId: 4, isGroup: true },
    { conversationId: "S:1688856756296405_337769972893279308", displayName: "李总", lastMessageTime: Math.floor(now / 1000) - 900, lastMessageId: 3, isGroup: false },
    { conversationId: "R:20738374660982927", displayName: "回收良品开发对接群", lastMessageTime: Math.floor(now / 1000) - 4200, lastMessageId: 2, isGroup: true },
    { conversationId: "R:30738374660982928", displayName: "费用卡片/折旧运行问题反馈", lastMessageTime: Math.floor(now / 1000) - 7200, lastMessageId: 1, isGroup: true },
    { conversationId: "S:1688856756296405_337769972893279309", displayName: "Neo 咨询", lastMessageTime: Math.floor(now / 1000) - 9600, lastMessageId: 1, isGroup: false },
    { conversationId: "R:40738374660982929", displayName: "开发BP沟通群", lastMessageTime: Math.floor(now / 1000) - 12000, lastMessageId: 1, isGroup: true },
  ];
  let highEqCurated = new Set([
    "R:10738374660982926",
    "S:1688856756296405_337769972893279308",
    "R:20738374660982927",
  ]);
  const highEqAliases = new Map();
  const highEqPromptIds = new Map([["R:10738374660982926", "eq_reply_customer"]]);
  const highEqPromptNotes = new Map([["R:10738374660982926", "对方是采购负责人，先安抚情绪，再给可执行的下一步。"]]);
  let highEqRevision = 3;
  const highEqChatView = (chat) => ({
    conversationId: chat.conversationId,
    displayName: highEqAliases.get(chat.conversationId) || chat.displayName,
    alias: highEqAliases.get(chat.conversationId) || "",
    promptId: highEqPromptIds.get(chat.conversationId) || "",
    promptNote: highEqPromptNotes.get(chat.conversationId) || "",
    available: true,
    lastMessageTime: chat.lastMessageTime,
    lastMessageId: chat.lastMessageId,
    isGroup: chat.isGroup,
  });
  const highEqCuratedChats = () =>
    [...highEqCurated]
      .map((id) => highEqCatalog.find((chat) => chat.conversationId === id))
      .filter(Boolean)
      .map(highEqChatView)
      .sort((left, right) => (right.lastMessageTime - left.lastMessageTime) || (right.lastMessageId - left.lastMessageId));
  const highEqCatalogChats = () =>
    highEqCatalog
      .map((chat) => ({
        ...highEqChatView(chat),
        added: highEqCurated.has(chat.conversationId),
      }))
      .sort((left, right) => (right.lastMessageTime - left.lastMessageTime) || (right.lastMessageId - left.lastMessageId));
  const highEqMessages = [
    { messageId: "m1", senderId: "1", senderName: "王小明", sendTime: Math.floor(now / 1000) - 2400, contentType: "text", text: "客户说订单导出一直转圈，是不是系统挂了？", images: [], imageCount: 0 },
    { messageId: "m2", senderId: "2", senderName: "我", sendTime: Math.floor(now / 1000) - 2100, contentType: "text", text: "我先让后端查一下慢查询，稍等。", images: [], imageCount: 0 },
    { messageId: "m3", senderId: "1", senderName: "王小明", sendTime: Math.floor(now / 1000) - 300, contentType: "text", text: "客户催得很急，能今天给个明确说法吗？", images: [], imageCount: 0 },
    { messageId: "m4", senderId: "1", senderName: "王小明", sendTime: Math.floor(now / 1000) - 120, contentType: "image", text: "图片", images: [{ index: 0, available: true, mimeType: "image/png", errorCode: "" }], imageCount: 1 },
  ];
  const highEqOlderMessages = Array.from({ length: 40 }, (_, index) => {
    const offset = 40 - index;
    return {
      messageId: "old-" + index,
      senderId: index % 3 === 0 ? "2" : "1",
      senderName: index % 3 === 0 ? "我" : "王小明",
      sendTime: Math.floor(now / 1000) - 3600 - offset * 600,
      contentType: "text",
      text: "更早的历史消息 " + String(offset).padStart(2, "0") + "：这是上周同步的进展和客户反馈，供上下文参考。",
      images: [],
      imageCount: 0,
    };
  });
  const highEqAllMessages = [...highEqOlderMessages, ...highEqMessages];
  const highEqOcr = {
    results: [
      { messageId: "m4", index: 0, text: "截图：订单导出失败，错误码 500，客户要求今天处理", source: "ocr", model: "qwen-vl-max", error: "" },
    ],
    engine: { available: true, source: "ocr", model: "qwen-vl-max" },
  };
  const wechatConfig = {
    detect: { found: true, running: true, dbDir: "E:/Documents/xwechat_files/wxid_xxx/db_storage", version: "4.0.3", accounts: [{ accountId: "wxid_demo", dbDir: "E:/Documents/xwechat_files/wxid_demo", root: "E:/Documents/xwechat_files/wxid_demo", selected: true }], selected: true, ready: true },
    projects: { projects: [
      { id: "p1", accountId: "wxid_demo", chatId: "wxid_boss", chatTitle: "李总", name: "李总", kind: "direct", enabled: true, prompt: "回复要稳重、简洁。", summary: "老板，关注交付进度", trigger: { mode: "auto" }, revision: 1, updatedAt: new Date(now).toISOString(), draft: { content: "李总，这个功能今天下午可以给您一个准确时间。", status: "ready", stale: false, errorCode: "", errorMessage: "", hasContent: true } },
      { id: "p2", accountId: "wxid_demo", chatId: "group_1", chatTitle: "产品项目群", name: "产品项目群", kind: "group", enabled: true, prompt: "", summary: "", trigger: { mode: "manual" }, revision: 1, updatedAt: new Date(now).toISOString(), draft: { content: "", status: "idle", stale: false, errorCode: "", errorMessage: "", hasContent: false } },
    ], defaultProviderId: "llm-default" },
    chats: { chats: [{ accountId: "wxid_demo", chatId: "wxid_boss", displayName: "李总", kind: "direct", lastMessage: "这个功能什么时候能上？", lastTimestamp: Math.floor(now / 1000), unread: 1 }, { accountId: "wxid_demo", chatId: "group_1", displayName: "产品项目群", kind: "group", lastMessage: "收到", lastTimestamp: Math.floor(now / 1000) - 300, unread: 0 }] },
    messages: { messages: [
      { id: "m1", projectId: "p1", kind: "text", senderName: "李总", direction: "incoming", text: "这个功能什么时候能上？客户在催了。", createTime: Math.floor(now / 1000) - 3600, media: [] },
      { id: "m2", projectId: "p1", kind: "text", senderName: "我", direction: "outgoing", text: "预计本周五给到测试版本。", createTime: Math.floor(now / 1000) - 3000, media: [] },
      { id: "m3", projectId: "p1", kind: "text", senderName: "李总", direction: "incoming", text: "能不能提前到周三？", createTime: Math.floor(now / 1000) - 1800, media: [] },
    ], hasMore: false },
    providers: { providers: [{ id: "llm-default", name: "默认模型（全局设置）", kind: "openai_compatible", baseUrl: "https://api.deepseek.com/v1", model: "deepseek-v4.1-flash", supportsVision: true }], mcpServers: [{ id: "m1", name: "本地知识库", enabled: true, transport: "http", toolCount: 3 }], defaultProviderId: "llm-default", llm: { provider: "openai_compatible", baseUrl: "https://api.deepseek.com/v1", model: "deepseek-v4.1-flash", configured: true, hasApiKey: true } },
    settings: { settings: { pollIntervalSeconds: 5, contextMessageLimit: 20, contextCharacterLimit: 6000, wechatDbDir: "E:/Documents/xwechat_files/wxid_demo/db_storage" } },
  };
  const invoke = async (command, payload) => {
    if (command === "bootstrap") return { config, configPath: "C:\\\\Users\\\\admin\\\\.wecom-issue-radar\\\\config.local.json", appVersion: "4.6.7" };
    if (command === "save_config") return { config, configPath: "", appVersion: "4.6.7" };
    if (command === "list_pending_smart_sheet_syncs") return [];
    if (command === "list_schedules") return config.schedules ?? [];
    if (command === "save_schedules") return (payload && payload.schedules) ?? [];
    if (command === "run_schedule_now") return null;
    if (command === "preview_smart_sheet") return { headers: ["问题描述", "模块"], rows: [["订单导出一直转圈", "订单/售后"]], warnings: [] };
    if (command === "sync_smart_sheet") return { success: true, written: 1, skipped: 0, failures: [] };
    if (command === "rescan_task_images") return { scanned: 0, resolved: 0, missing: 0 };
    if (command === "list_schedule_execution_history") return { items: [{ executionId: "e1", scheduleId: "daily", scheduleName: "每日群聊导出", trigger: "automatic", startedAt: "2026-07-28T07:56:00", finishedAt: "2026-07-28T07:57:00", success: true, status: "success", message: "成功" }, { executionId: "e2", scheduleId: "daily", scheduleName: "每日群聊导出", trigger: "manual", startedAt: "2026-07-27T08:24:00", finishedAt: "2026-07-27T08:25:00", success: true, status: "success", message: "成功" }], page: 1, pageSize: 5, total: 2, totalPages: 1 };
    if (command === "reply_runtime_query") {
      // bridge.replyRuntimeQuery sends { query: { protocolVersion, body } }.
      const query = payload && payload.query ? payload.query : payload;
      const body = (query && query.body) || {};
      const kind = body.kind;
      if (kind === "runtime.snapshot") return runtimeSnapshot;
      if (kind === "listener.list") return { listeners: emptyListeners ? [] : listeners, revision: 1 };
      if (kind === "work.list") {
        if (emptyListeners) return { items: [], total: 0, page: 1, pageSize: 20, revision: 1 };
        const bucket = body.bucket;
        const items = bucket === "pending"
          ? workList.filter((w) => w.status === "pending")
          : bucket === "history"
            ? workList.filter((w) => w.status === "sent" || w.status === "failed")
            : workList.filter((w) => w.status === "waiting" || w.status === "working");
        return { items, total: items.length, page: 1, pageSize: 20, revision: 1 };
      }
      if (kind === "work.detail") {
        const workId = body.workId;
        return { item: workList.find((w) => w.id === workId) ?? workList[0] };
      }
      if (kind === "mcp.list") return { servers: [] };
      if (kind === "mcp.catalog") return { servers: [] };
      if (kind === "dify.list") return { apps: [] };
      if (kind === "listener.options") return { mcpServers: [], difyApps: [] };
      if (kind === "chat.recent") {
        return {
          chats: highEqCuratedChats(),
          revision: highEqRevision,
          selfSenderId: "2",
          selfSenderName: "我",
        };
      }
      if (kind === "chat.catalog") {
        return {
          chats: highEqCatalogChats(),
          revision: highEqRevision,
          selfSenderId: "2",
          selfSenderName: "我",
        };
      }
      if (kind === "chat.context") {
        const chat = highEqCatalog.find((item) => item.conversationId === body.conversationId) ?? highEqCatalog[0];
        const limit = Math.max(1, Math.min(Number(body.limit) || 20, 50));
        const before = Number(body.before) || 0;
        const all = highEqAllMessages.slice().sort((left, right) => left.sendTime - right.sendTime);
        const eligible = before > 0 ? all.filter((message) => message.sendTime <= before) : all;
        const page = eligible.slice(-limit);
        return {
          conversationId: chat.conversationId,
          displayName: highEqAliases.get(chat.conversationId) || chat.displayName,
          isGroup: chat.isGroup,
          windowFallback: false,
          hasMore: eligible.length > page.length,
          selfSenderId: "2",
          messages: page,
        };
      }
      if (kind === "chat.ocr") return highEqOcr;
      return {};
    }
    if (command === "reply_runtime_execute") {
      const command = (payload && payload.command) || payload || {};
      const body = (command && command.body) || {};
      const kind = body.kind;
      if (kind === "chat.chats.add") {
        for (const item of Array.isArray(body.chats) ? body.chats : []) {
          const conversationId = String(item.conversationId || "");
          if (!conversationId) continue;
          highEqCurated.add(conversationId);
          if (item.alias) highEqAliases.set(conversationId, String(item.alias).trim());
        }
        highEqRevision += 1;
        return { revision: highEqRevision };
      }
      if (kind === "chat.chats.rename") {
        const conversationId = String(body.conversationId || "");
        const alias = String(body.alias || "").trim();
        if (conversationId) {
          if (alias) highEqAliases.set(conversationId, alias);
          else highEqAliases.delete(conversationId);
        }
        highEqRevision += 1;
        return { revision: highEqRevision };
      }
      if (kind === "chat.chats.remove") {
        for (const conversationId of Array.isArray(body.conversationIds) ? body.conversationIds : []) {
          highEqCurated.delete(String(conversationId || ""));
          highEqPromptIds.delete(String(conversationId || ""));
          highEqPromptNotes.delete(String(conversationId || ""));
        }
        highEqRevision += 1;
        return { revision: highEqRevision };
      }
      if (kind === "chat.prompt.set") {
        const conversationId = String(body.conversationId || "");
        if (conversationId) {
          if (Object.prototype.hasOwnProperty.call(body, "promptId")) {
            const promptId = String(body.promptId || "").trim();
            if (promptId) highEqPromptIds.set(conversationId, promptId);
            else highEqPromptIds.delete(conversationId);
          }
          if (Object.prototype.hasOwnProperty.call(body, "promptNote")) {
            const promptNote = String(body.promptNote || "").trim();
            if (promptNote) highEqPromptNotes.set(conversationId, promptNote);
            else highEqPromptNotes.delete(conversationId);
          }
        }
        highEqRevision += 1;
        return { revision: highEqRevision };
      }
      return { revision: highEqRevision };
    }
    if (command === "list_groups") return { groups: [{ conversation_id: "1", display_name: "OA开发对接群（九机+九讯云）" }, { conversation_id: "2", display_name: "回收良品开发对接群" }, { conversation_id: "3", display_name: "泛中台对接" }] };
    if (command === "detect_environment") return { running: true, executablePaths: ["C:/Program Files/WXWork/WXWork.exe"], dataDirectories: ["E:/Documents/WXWork/1688856756296405/Data"] };
    if (command === "updater_check") return null;
    if (command === "wechat_action") {
      const action = payload && payload.action;
      const map = { detect: wechatConfig.detect, chats: wechatConfig.chats, projects: wechatConfig.projects, messages: wechatConfig.messages, providers: wechatConfig.providers, settings: wechatConfig.settings, poll: { projects: [], updated: 0, inserted: 0 }, bootstrap: { detect: wechatConfig.detect, workspace: { projects: wechatConfig.projects.projects, defaultProviderId: "llm-default" }, settings: wechatConfig.settings, providers: wechatConfig.providers } };
      if (action in map) return map[action];
      if (action === "draft_generate" || action === "draft_rewrite" || action === "draft_save") return { draft: { projectId: "p1", content: "李总，我理解客户比较着急。我今晚先出一版可演示的版本，周三上午给您确认；如果还有风险我会提前同步。", status: "ready", stale: false } };
      if (action === "export") return { dir: "E:/Documents/WeComIssueRadar/wechat-export", files: ["chat.md", "chat.json", "media/"] };
      return {};
    }
    if (command === "assistant_action") {
      if (payload.action === "eq_reply") return {
        styles: [
          { id: "steady", title: "稳重", content: "李总，我理解客户比较着急。我先确认当前进度，今天下班前给您一个明确的时间点；如果存在风险我会提前同步。" },
          { id: "warm", title: "亲和", content: "李总，收到，我知道客户那边在等。我马上跟进一版可演示的进度，今天内给您准信，辛苦您先安抚一下客户。" },
          { id: "brief", title: "简短", content: "李总，我确认下进度，今天下班前给您明确时间。" },
        ],
        interpretation: { summary: "对方在催进度，并暗示客户压力已经传到他这里。", signals: [
          { label: "时间压力", detail: "“什么时候能上”“客户在催”说明需要一个明确时间点。" },
          { label: "责任转移", detail: "把客户压力同步给你，希望你给出承诺。" },
        ] },
        check: { risks: ["只给“尽快”会显得没有掌控感"], suggestions: ["给出具体时间点", "说明如果延期会提前同步"] },
        fallback: false,
      };
      return {};
    }
    if (command === "open_path" || command === "open_agent_log_directory" || command === "open_documentation") return null;
    if (command === "window_minimize" || command === "window_toggle_maximize" || command === "window_close") return null;
    if (command === "zoom_get") return 1;
    return null;
  };
  const events = new Map();
  window.desktop = {
    platform: "win32",
    versions: { app: "4.6.7", electron: "45.0.0", chrome: "140", node: "24" },
    invoke,
    on(event, handler) { if (!events.has(event)) events.set(event, new Set()); events.get(event).add(handler); return () => events.get(event)?.delete(handler); },
  };
})();
`;

const routes = [
  { name: "workbench", steps: [] },
  { name: "wecom-issue", steps: ["企业微信", "问题雷达"] },
  { name: "wecom-schedules", steps: ["企业微信", "定时任务"] },
  { name: "wecom-reply", steps: ["企业微信", "群监听回复"] },
  { name: "wecom-listener-editor", steps: ["企业微信", "群监听回复", "新增监听"] },
  { name: "wechat", steps: ["个人微信", "会话草稿"] },
  { name: "wechat-config", steps: ["个人微信", "配置中心"] },
  { name: "wecom-high-eq-reply", steps: ["企业微信", "高情商回复"] },
  { name: "strategy-prompts", steps: ["策略与发布", "提示词库"] },
  { name: "strategy-publish", steps: ["策略与发布", "发布目标"] },
  { name: "strategy-publish-tencent", steps: ["策略与发布", "发布目标", "默认问题清单"] },
  { name: "strategy-publish-mapping", steps: ["策略与发布", "发布目标", "默认问题清单", "字段映射"] },
  { name: "settings", steps: ["设置", "常用与高级"] },
];

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
      }
    });
  }
  send(method, params = {}, sessionId) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
}

async function connect(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  return new Cdp(socket);
}

async function clickByText(cdp, sessionId, text) {
  const expression = `(() => {
    const nodes = [...document.querySelectorAll("button, a")];
    const target = nodes.find((node) => (node.textContent || "").trim().includes(${JSON.stringify(text)}));
    if (!target) return false;
    target.click();
    return true;
  })()`;
  return cdp.send("Runtime.evaluate", { expression, returnByValue: true }, sessionId);
}

async function evaluate(cdp, sessionId, expression) {
  const result = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }, sessionId);
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "evaluate failed");
  return result.result?.value;
}

async function pressKey(cdp, sessionId, key, modifiers = 0) {
  const codeByKey = { Escape: 27, k: 75, s: 83, Enter: 13, Tab: 9, ArrowDown: 40, ArrowUp: 38 };
  const base = { key, code: key, modifiers, windowsVirtualKeyCode: codeByKey[key] ?? 0, nativeVirtualKeyCode: codeByKey[key] ?? 0 };
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", ...base }, sessionId);
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", ...base }, sessionId);
}

/**
 * Interaction audit used by the UI rounds: every check is an observable
 * behaviour, so before/after runs can be compared without reading code.
 */
async function runAudit(cdp, sessionId, url) {
  const audit = {};
  const reset = async () => {
    await cdp.send("Page.navigate", { url }, sessionId);
    await wait(2000);
  };

  // 1. Page scroll position and focus after in-app navigation.
  await reset();
  await clickByText(cdp, sessionId, "设置");
  await wait(400);
  await clickByText(cdp, sessionId, "常用与高级");
  await wait(500);
  await evaluate(cdp, sessionId, `(() => { const f = document.querySelector('.page-frame'); if (!f) return -1; f.scrollTop = 700; return f.scrollTop; })()`);
  await evaluate(cdp, sessionId, `(() => { const link = document.querySelector('.skip-link'); if (link) link.click(); return true; })()`);
  await wait(200);
  audit.skipLinkActivates = await evaluate(cdp, sessionId, `(() => { const f = document.querySelector('.page-frame'); return { scrollTop: f ? f.scrollTop : -1, focused: document.activeElement ? document.activeElement.id : null }; })()`);
  await clickByText(cdp, sessionId, "企业微信");
  await wait(600);
  audit.scrollReset = await evaluate(cdp, sessionId, `(() => { const f = document.querySelector('.page-frame'); return f ? f.scrollTop : -1; })()`);
  audit.focusAfterNav = await evaluate(cdp, sessionId, `(() => { const a = document.activeElement; return a ? { tag: a.tagName, id: a.id, cls: String(a.className || '').slice(0, 60) } : null; })()`);

  // 2. Landmarks and current-page semantics.
  audit.ariaCurrentCount = await evaluate(cdp, sessionId, `document.querySelectorAll('[aria-current="page"]').length`);
  audit.skipLink = await evaluate(cdp, sessionId, `!!document.querySelector('a[href="#main-content"]')`);

  // 3. Command palette shortcut.
  await pressKey(cdp, sessionId, "k", 2);
  await wait(500);
  audit.quickSwitcher = await evaluate(cdp, sessionId, `!!document.querySelector('[data-quick-switcher]')`);
  if (audit.quickSwitcher) {
    const paletteShot = await cdp.send("Page.captureScreenshot", { format: "png" }, sessionId);
    await writeFile(path.join(outDir, "quick-switcher.png"), Buffer.from(paletteShot.data, "base64"));
    await pressKey(cdp, sessionId, "Escape");
    await wait(300);
    audit.quickSwitcherClosesOnEscape = await evaluate(cdp, sessionId, `!document.querySelector('[data-quick-switcher]')`);
    await pressKey(cdp, sessionId, "k", 2);
    await wait(400);
    await pressKey(cdp, sessionId, "ArrowDown");
    await wait(200);
    audit.quickSwitcherArrow = await evaluate(cdp, sessionId, `(() => {
      const active = document.querySelector('.quick-switcher-item.is-active');
      return active ? active.textContent.trim().slice(0, 20) : null;
    })()`);
    await pressKey(cdp, sessionId, "Enter");
    await wait(700);
    audit.quickSwitcherNavigates = await evaluate(cdp, sessionId, `(() => {
      const palette = document.querySelector('[data-quick-switcher]');
      const main = document.querySelector('#main-content');
      return { closed: !palette, label: main ? main.getAttribute('aria-label') : null };
    })()`);
  }

  // 4. Nested scroll containers on the WeChat config page.
  await reset();
  await clickByText(cdp, sessionId, "个人微信");
  await wait(500);
  await clickByText(cdp, sessionId, "配置中心");
  await wait(900);
  audit.wechatConfigScroll = await evaluate(cdp, sessionId, `(() => {
    const frame = document.querySelector('.page-frame');
    const page = document.querySelector('.wechat-config-page');
    return {
      frameScrolls: frame ? frame.scrollHeight > frame.clientHeight + 4 : null,
      configScrolls: page ? page.scrollHeight > page.clientHeight + 4 : null,
      pageOverflow: page ? getComputedStyle(page).overflowY : null,
    };
  })()`);

  // 5. Drawer keyboard behaviour: focus entry, focus containment, Esc close.
  await reset();
  await clickByText(cdp, sessionId, "个人微信");
  await wait(500);
  await clickByText(cdp, sessionId, "会话草稿");
  await wait(900);
  const drawerOpened = await evaluate(cdp, sessionId, `(() => {
    const button = document.querySelector('[title="新建会话项目"]');
    if (!button) return false;
    button.click();
    return true;
  })()`);
  await wait(700);
  const drawerShot = await cdp.send("Page.captureScreenshot", { format: "png" }, sessionId);
  await writeFile(path.join(outDir, "drawer.png"), Buffer.from(drawerShot.data, "base64"));
  audit.drawer = {
    opened: drawerOpened,
    present: await evaluate(cdp, sessionId, `!!document.querySelector('.wechat-drawer')`),
    focusInside: await evaluate(cdp, sessionId, `(() => {
      const drawer = document.querySelector('.wechat-drawer');
      return !!drawer && drawer.contains(document.activeElement);
    })()`),
  };
  await pressKey(cdp, sessionId, "Escape");
  await wait(400);
  audit.drawer.closedOnEscape = await evaluate(cdp, sessionId, `!document.querySelector('.wechat-drawer')`);

  // 6. Reduced-motion support for the page entrance animation.
  await cdp.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] }, sessionId);
  audit.reducedMotionAnimation = await evaluate(cdp, sessionId, `(() => {
    const first = document.querySelector('.page-frame > *');
    return first ? getComputedStyle(first).animationName : null;
  })()`);
  await cdp.send("Emulation.setEmulatedMedia", { features: [] }, sessionId);

  return audit;
}

async function main() {
  await mkdir(outDir, { recursive: true });
  const profile = await mkdtemp(path.join(tmpdir(), "ui-preview-"));
  const edge = spawn(EDGE, [
    "--headless=new",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-component-update",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-service-autorun",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--window-size=1280,820",
    "about:blank",
  ], { stdio: "ignore", windowsHide: true });

  try {
    let version;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      try {
        version = await fetchJson(`http://127.0.0.1:${port}/json/version`);
        break;
      } catch {
        await wait(200);
      }
    }
    if (!version) throw new Error("Edge CDP endpoint did not start");
    const cdp = await connect(version.webSocketDebuggerUrl);
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Runtime.enable", {}, sessionId);
    cdp.socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.method === "Runtime.exceptionThrown") {
        const detail = message.params?.exceptionDetails;
        const frames = (detail?.stackTrace?.callFrames || [])
          .slice(0, 4)
          .map((frame) => `${frame.functionName || "(anonymous)"}@${frame.url.split("/").pop()}:${frame.lineNumber + 1}`)
          .join(" <- ");
        console.warn("page exception:", detail?.exception?.description || detail?.text, frames);
      }
    });
    await cdp.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false }, sessionId);
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: mockBridge }, sessionId);
    await cdp.send("Page.navigate", { url: pageUrl }, sessionId);
    await wait(2500);
    // Guard against attaching to an unrelated Edge instance that already owns
    // the requested debugging port (it would silently capture the wrong app).
    const landed = await evaluate(cdp, sessionId, "location.href");
    if (!String(landed).startsWith(APP_URL.split("?")[0])) {
      throw new Error(`preview landed on unexpected page: ${landed}`);
    }

    const selected = only.length ? routes.filter((route) => only.includes(route.name)) : routes;
    const a11yReport = {};
    for (const route of selected) {
      await cdp.send("Page.navigate", { url: pageUrl }, sessionId);
      await wait(1800);
      for (const text of route.steps) {
        const result = await clickByText(cdp, sessionId, text);
        if (!result.result?.value) console.warn(`click missed: ${text}`);
        await wait(350);
      }
      await wait(900);
      const shot = await cdp.send("Page.captureScreenshot", { format: "png" }, sessionId);
      const file = path.join(outDir, `${route.name}${dark ? "-dark" : ""}.png`);
      await writeFile(file, Buffer.from(shot.data, "base64"));
      console.log(`saved ${file}`);

      // Cheap a11y signal: visible controls without any accessible name and
      // images without alt text. Keeps regressions visible without a full DOM
      // audit dependency.
      a11yReport[route.name] = await evaluate(cdp, sessionId, `(() => {
        const visible = (node) => node.offsetParent !== null || node.getClientRects().length > 0;
        const name = (node) => (node.textContent || "").trim()
          || node.getAttribute("aria-label") || node.getAttribute("title") || "";
        const unnamedButtons = [...document.querySelectorAll("button, [role='button']")]
          .filter(visible)
          .filter((node) => !name(node))
          .map((node) => node.outerHTML.replace(/\\s+/g, " ").slice(0, 140));
        const imagesWithoutAlt = [...document.querySelectorAll("img")]
          .filter(visible)
          .filter((node) => !node.getAttribute("alt"))
          .map((node) => node.outerHTML.replace(/\\s+/g, " ").slice(0, 140));
        return { unnamedButtons, imagesWithoutAlt };
      })()`);
      if (a11yReport[route.name].unnamedButtons.length || a11yReport[route.name].imagesWithoutAlt.length) {
        console.warn(`a11y warnings on ${route.name}:`, JSON.stringify(a11yReport[route.name]));
      }
    }
    await writeFile(path.join(outDir, "a11y.json"), `${JSON.stringify(a11yReport, null, 2)}\n`);
    if (audit) {
      const result = await runAudit(cdp, sessionId, pageUrl);
      await writeFile(path.join(outDir, "audit.json"), `${JSON.stringify(result, null, 2)}\n`);
      console.log("interaction audit:", JSON.stringify(result));
    }
    await cdp.send("Target.closeTarget", { targetId });
  } finally {
    edge.kill();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

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
  llm: { provider: "openai_compatible", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", api_key: "sk-demo", model: "qwen-plus", temperature: 0.1 },
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
  const listeners = [
    { id: "l1", revision: 1, name: "OA开发对接群（九机+九讯云）自动答疑", enabled: false, groupId: "1", groupName: "OA开发对接群（九机+九讯云）", answerEngine: "mcp", runtimeVersion: "v2", difyAppId: "", systemPrompt: "", webhookConfigured: true, webhookVerified: true, deliveryMode: "review", requireIndependentReview: true, reviewPrompt: "", tools: [], tuning: { pollIntervalSeconds: 5, sameSenderMergeSeconds: 30, humanReplyWaitSeconds: 20, sessionTimeoutSeconds: 300, maxConcurrency: 2, mcpTimeoutSeconds: 60, difyTimeoutSeconds: 60, maxAgentRounds: 6, maxToolCalls: 20 }, health: "stopped", pendingCount: 0, toolSafety: { readOnly: 27, unknown: 0, write: 0, destructive: 0, blocked: 0, bootstrap: 4 } },
    { id: "l2", revision: 1, name: "回收良品开发对接群自动答疑", enabled: false, groupId: "2", groupName: "回收良品开发对接群", answerEngine: "mcp", runtimeVersion: "legacy", difyAppId: "", systemPrompt: "", webhookConfigured: true, webhookVerified: true, deliveryMode: "review", requireIndependentReview: true, reviewPrompt: "", tools: [], tuning: { pollIntervalSeconds: 5, sameSenderMergeSeconds: 30, humanReplyWaitSeconds: 20, sessionTimeoutSeconds: 300, maxConcurrency: 2, mcpTimeoutSeconds: 60, difyTimeoutSeconds: 60, maxAgentRounds: 6, maxToolCalls: 20 }, health: "stopped", pendingCount: 0, toolSafety: { readOnly: 27, unknown: 0, write: 0, destructive: 0, blocked: 0, bootstrap: 4 } },
    { id: "l3", revision: 1, name: "泛中台对接", enabled: false, groupId: "3", groupName: "泛中台对接", answerEngine: "mcp", runtimeVersion: "v2", difyAppId: "", systemPrompt: "", webhookConfigured: true, webhookVerified: true, deliveryMode: "review", requireIndependentReview: true, reviewPrompt: "", tools: [], tuning: { pollIntervalSeconds: 5, sameSenderMergeSeconds: 30, humanReplyWaitSeconds: 20, sessionTimeoutSeconds: 300, maxConcurrency: 2, mcpTimeoutSeconds: 60, difyTimeoutSeconds: 60, maxAgentRounds: 6, maxToolCalls: 20 }, health: "stopped", pendingCount: 0, toolSafety: { readOnly: 27, unknown: 0, write: 0, destructive: 0, blocked: 0, bootstrap: 4 } },
    { id: "l4", revision: 1, name: "财务开发组自动答疑", enabled: false, groupId: "4", groupName: "财务开发组", answerEngine: "mcp", runtimeVersion: "v2", difyAppId: "", systemPrompt: "", webhookConfigured: true, webhookVerified: true, deliveryMode: "review", requireIndependentReview: true, reviewPrompt: "", tools: [], tuning: { pollIntervalSeconds: 5, sameSenderMergeSeconds: 30, humanReplyWaitSeconds: 20, sessionTimeoutSeconds: 300, maxConcurrency: 2, mcpTimeoutSeconds: 60, difyTimeoutSeconds: 60, maxAgentRounds: 6, maxToolCalls: 20 }, health: "stopped", pendingCount: 0, toolSafety: { readOnly: 27, unknown: 0, write: 0, destructive: 0, blocked: 0, bootstrap: 4 } },
  ];
  const runtimeSnapshot = { running: true, pendingCount: 0, activeRetrievals: 0, queuedRetrievals: 0, listeners, recentFailures: 56 };
  const work = (id, status, question, answer) => ({ id: "w" + id, version: 1, listenerId: "l1", listenerName: "OA开发对接群", groupId: "1", groupName: "OA开发对接群", senderName: "王小明", question, answer, status, stage: status === "waiting" ? "human_wait" : "ready", createdAt: new Date(now - id * 60000).toISOString(), updatedAt: new Date(now - id * 60000).toISOString(), imageStatus: "none" });
  const workList = [work(1, "waiting", "客户说订单导出一直转圈，是不是系统挂了？", ""), work(2, "pending", "回收模块的称重数据对不上，能帮忙看下吗？", "已收到，我先查一下称重接口的日志。"), work(3, "waiting", "这个月对账单什么时候能出？", "")];
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
    providers: { providers: [{ id: "llm-default", name: "通义千问", kind: "openai_compatible", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus", supportsVision: true }], mcpServers: [{ id: "m1", name: "本地知识库", enabled: true, transport: "http", toolCount: 3 }], defaultProviderId: "llm-default" },
    settings: { settings: { pollIntervalSeconds: 5, contextMessageLimit: 20, contextCharacterLimit: 6000, wechatDbDir: "E:/Documents/xwechat_files/wxid_demo/db_storage" } },
  };
  const invoke = async (command, payload) => {
    if (command === "bootstrap") return { config, configPath: "C:\\\\Users\\\\admin\\\\.wecom-issue-radar\\\\config.local.json", appVersion: "4.1.0" };
    if (command === "save_config") return { config, configPath: "", appVersion: "4.1.0" };
    if (command === "list_pending_smart_sheet_syncs") return [];
    if (command === "list_schedule_execution_history") return { items: [{ executionId: "e1", scheduleId: "daily", scheduleName: "每日群聊导出", trigger: "automatic", startedAt: "2026-07-28T07:56:00", finishedAt: "2026-07-28T07:57:00", success: true, status: "success", message: "成功" }, { executionId: "e2", scheduleId: "daily", scheduleName: "每日群聊导出", trigger: "manual", startedAt: "2026-07-27T08:24:00", finishedAt: "2026-07-27T08:25:00", success: true, status: "success", message: "成功" }], page: 1, pageSize: 5, total: 2, totalPages: 1 };
    if (command === "reply_runtime_query") {
      const kind = payload && payload.body && payload.body.kind;
      if (kind === "runtime.snapshot") return runtimeSnapshot;
      if (kind === "listener.list") return { listeners };
      if (kind === "work.list") { const bucket = payload.body.bucket; return { items: bucket === "pending" ? workList.filter((w) => w.status === "pending") : bucket === "history" ? [] : workList.filter((w) => w.status === "waiting"), total: 3, page: 1, pageSize: 20 }; }
      if (kind === "mcp.list") return { servers: [] };
      if (kind === "mcp.catalog") return { servers: [] };
      if (kind === "dify.list") return { apps: [] };
      if (kind === "listener.options") return { mcpServers: [], difyApps: [] };
      return {};
    }
    if (command === "list_groups") return { groups: [{ conversation_id: "1", display_name: "OA开发对接群（九机+九讯云）" }, { conversation_id: "2", display_name: "回收良品开发对接群" }, { conversation_id: "3", display_name: "泛中台对接" }] };
    if (command === "detect_environment") return { running: true, executablePaths: ["C:/Program Files/WXWork/WXWork.exe"], dataDirectories: ["E:/Documents/WXWork/1688856756296405/Data"] };
    if (command === "updater_check") return null;
    if (command === "wechat_action") {
      const action = payload && payload.action;
      const map = { detect: wechatConfig.detect, chats: wechatConfig.chats, projects: wechatConfig.projects, messages: wechatConfig.messages, providers: wechatConfig.providers, settings: wechatConfig.settings, poll: { projects: [], updated: 0, inserted: 0 } };
      if (action in map) return map[action];
      if (action === "draft_generate" || action === "draft_rewrite" || action === "draft_save") return { draft: { projectId: "p1", content: "李总，我理解客户比较着急。我今晚先出一版可演示的版本，周三上午给您确认；如果还有风险我会提前同步。", status: "ready", stale: false } };
      if (action === "export") return { dir: "E:/Documents/WeComIssueRadar/wechat-export", files: ["chat.md", "chat.json", "media/"] };
      return {};
    }
    if (command === "search_action") {
      if (payload.action === "status") return { indexed: 12846, total: 12846, building: false, progress: 1, lastIndexedAt: new Date(now).toISOString(), error: "" };
      if (payload.action === "query") return { items: [
        { id: "1", source: "wecom", convId: "1", convName: "OA开发对接群", kind: "text", senderName: "王小明", text: "客户反馈订单导出一直转圈，麻烦看下导出接口。", ts: Math.floor(now / 1000) - 8000, msgId: "101" },
        { id: "2", source: "wechat", convId: "p1", convName: "李总", kind: "text", senderName: "李总", text: "这个功能什么时候能上？客户在催了。", ts: Math.floor(now / 1000) - 3600, msgId: "102" },
        { id: "3", source: "wecom", convId: "2", convName: "回收良品开发对接群", kind: "text", senderName: "赵敏", text: "回收模块的称重数据对不上，怀疑是同步延迟。", ts: Math.floor(now / 1000) - 7200, msgId: "103" },
      ], total: 3 };
      if (payload.action === "context") return { messages: [{ id: "1", senderName: "王小明", text: "客户反馈订单导出一直转圈，麻烦看下导出接口。", ts: Math.floor(now / 1000) - 8000, direction: "incoming" }] };
      return {};
    }
    if (command === "daily_action") {
      if (payload.action === "eq_reply") return { styles: [
        { id: "steady", title: "稳重", content: "李总，我理解客户比较着急。我先确认当前进度，今天下班前给您一个明确的时间点；如果存在风险我会提前同步。" },
        { id: "warm", title: "亲和", content: "李总，收到，我知道客户那边在等。我马上跟进一版可演示的进度，今天内给您准信，辛苦您先安抚一下客户。" },
        { id: "brief", title: "简短", content: "李总，我确认下进度，今天下班前给您明确时间。" },
      ], fallback: false };
      if (payload.action === "weekly") return { draft: "# 本周工作周报\\n\\n## 进展\\n- 订单导出性能问题完成排查，定位到慢查询。\\n- 回收称重数据同步完成修复。\\n\\n## 问题\\n- 客户催办功能上线时间，需周三前给出演示版本。\\n\\n## 下周计划\\n- 完成导出优化并回归。\\n- 跟进财务对账单需求。", stats: { messages: 326, conversations: 8 }, fallback: false };
      if (payload.action === "todos" || payload.action === "todos_refresh") return { created: 2, items: [
        { id: "t1", title: "给李总一个周三演示版本", due: "2026-10-23", source: "wechat", convName: "李总", senderName: "李总", createdAt: new Date(now).toISOString(), done: false, confidence: 0.9 },
        { id: "t2", title: "核对回收称重同步延迟", due: "2026-10-24", source: "wecom", convName: "回收良品开发对接群", senderName: "赵敏", createdAt: new Date(now).toISOString(), done: false, confidence: 0.75 },
      ] };
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
    versions: { app: "4.1.0", electron: "45.0.0", chrome: "140", node: "24" },
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
  { name: "wechat", steps: ["个人微信", "会话草稿"] },
  { name: "daily-search", steps: ["日常助手", "聊天查找"] },
  { name: "daily-reply", steps: ["日常助手", "高情商回复"] },
  { name: "daily-report", steps: ["日常助手", "周报总结"] },
  { name: "daily-todos", steps: ["日常助手", "待办与承诺"] },
  { name: "strategy-prompts", steps: ["策略与发布", "提示词库"] },
  { name: "strategy-publish", steps: ["策略与发布", "发布目标"] },
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

async function main() {
  await mkdir(outDir, { recursive: true });
  const profile = await mkdtemp(path.join(tmpdir(), "ui-preview-"));
  const edge = spawn(EDGE, [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--remote-debugging-port=9222",
    `--user-data-dir=${profile}`,
    "--window-size=1280,820",
    "about:blank",
  ], { stdio: "ignore", windowsHide: true });

  try {
    let version;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      try {
        version = await fetchJson("http://127.0.0.1:9222/json/version");
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
        console.warn("page exception:", detail?.exception?.description || detail?.text);
      }
    });
    await cdp.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false }, sessionId);
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: mockBridge }, sessionId);
    await cdp.send("Page.navigate", { url: pageUrl }, sessionId);
    await wait(2500);

    const selected = only.length ? routes.filter((route) => only.includes(route.name)) : routes;
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

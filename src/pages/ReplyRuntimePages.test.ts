import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  buildListenerSaveBody,
  buildWorkActionBody,
  defaultListenerDraft,
  mcpCatalogAllowsGrants,
  mcpCatalogUnavailableLabel,
  parseRecordSecretEdit,
  runtimeAvailabilityCopy,
  tuningValidationErrors,
} from "../lib/replyRuntimeUi";
import { difyAppFromWire } from "./DifyAppsPage";
import { listenerFromWire, listenerSaveResultFromWire } from "./GroupReplyPage";

/**
 * Group reply markup and handlers were split into `./groupreply/**` (M1 task
 * 3.6). String assertions below intentionally cover the combined source so the
 * visibility contract keeps its original strength.
 */
const GROUP_REPLY_SOURCE_FILES = [
  "./GroupReplyPage.tsx",
  "./groupreply/shared.ts",
  "./groupreply/common.tsx",
  "./groupreply/ListenerList.tsx",
  "./groupreply/WorkQueue.tsx",
  "./groupreply/ListenerEditorDrawer.tsx",
  "./groupreply/WorkDetailDrawer.tsx",
  "./groupreply/useGroupReplyData.ts",
];

function readGroupReplySources(): string {
  return GROUP_REPLY_SOURCE_FILES
    .map((file) => readFileSync(fileURLToPath(new URL(file, import.meta.url)), "utf8"))
    .join("\n");
}

describe("listener editor policy", () => {
  it("only treats enabled, healthy MCP catalogs as grantable", () => {
    const enabled = { enabled: true, catalog: { error: null } };
    expect(mcpCatalogAllowsGrants(enabled, { error: null })).toBe(true);
    expect(mcpCatalogAllowsGrants({ enabled: false }, { error: null })).toBe(false);
    expect(mcpCatalogAllowsGrants({ enabled: true, catalog: { error: { code: "FAILED" } } }, { error: null })).toBe(false);
    expect(mcpCatalogAllowsGrants(enabled, { error: { code: "FAILED" } })).toBe(false);
    expect(mcpCatalogUnavailableLabel(enabled, { error: { code: "FAILED" } })).toBe("目录已过期 · 不可授权");
  });

  it.each([
    ["tool_grant_invalidated", "权限因 MCP 配置或目录变化而失效"],
    ["server_unhealthy", "MCP 服务最近一次连接测试失败"],
    ["rediscovery_required", "重新测试服务以发现工具"],
  ])("shows %s as an actionable degraded listener", (status, expectedReason) => {
    const listener = listenerFromWire({
      id: "listener-1",
      name: "售后群答疑",
      enabled: true,
      groupId: "R:support",
      health: { status },
    });

    expect(listener.health).toBe("degraded");
    expect(listener.healthMessage).toContain(expectedReason);
  });

  it("preserves the backend's concrete group-message polling failure", () => {
    const listener = listenerFromWire({
      id: "listener-1",
      name: "售后群答疑",
      enabled: true,
      groupId: "R:support",
      health: {
        status: "error",
        message: "WAL index frame checksum does not match WAL commit",
      },
    });

    expect(listener.health).toBe("error");
    expect(listener.healthMessage).toBe("WAL index frame checksum does not match WAL commit");
  });

  it("serializes an Agent V2 listener policy without per-tool grants", () => {
    const draft = {
      ...defaultListenerDraft(),
      id: "listener-1",
      name: "售后群答疑",
      enabled: true,
      groupId: "R:after-sales",
      groupName: "售后群",
      deliveryMode: "review" as const,
    };
    const body = buildListenerSaveBody({
      draft,
      webhookEdit: { mode: "keep" },
    });

    expect(body).toEqual({
      kind: "listener.save",
      listener: {
        id: "listener-1",
        name: "售后群答疑",
        enabled: true,
        groupId: "R:after-sales",
        groupName: "售后群",
        answerEngine: "mcp",
        runtimeVersion: "v2",
        difyAppId: "",
        systemPrompt: draft.systemPrompt,
        pollIntervalSeconds: 5,
        sameSenderMergeSeconds: 20,
        humanReplyWaitSeconds: 120,
        sessionTimeoutSeconds: 1800,
        maxConcurrency: 4,
        mcpTimeoutSeconds: 900,
        difyTimeoutSeconds: 300,
        maxAgentRounds: 6,
        maxToolCalls: 200,
        autoSend: false,
        requireIndependentReview: true,
        reviewPrompt: "",
      },
      secretPatch: { webhookUrl: { mode: "keep" } },
    });
    expect(body.listener).not.toHaveProperty("toolGrants");
  });

  it("serializes Dify listeners without any MCP tool payload", () => {
    const draft = {
      ...defaultListenerDraft(),
      name: "库存群答疑",
      groupId: "R:stock",
      groupName: "库存群",
      answerEngine: "dify" as const,
      difyAppId: "stock-flow",
    };
    const body = buildListenerSaveBody({
      draft,
      webhookEdit: { mode: "keep" },
    });

    expect(body.listener).toMatchObject({
      answerEngine: "dify",
      difyAppId: "stock-flow",
      difyTimeoutSeconds: 300,
    });
    expect(body.listener).not.toHaveProperty("toolGrants");
  });

  it("normalizes older listeners to Agent V2 and preserves Dify routing fields", () => {
    const legacy = listenerFromWire({
      id: "legacy-listener",
      name: "旧监听器",
      groupId: "R:legacy",
      runtimeVersion: "legacy",
    });
    const dify = listenerFromWire({
      id: "dify-listener",
      name: "Dify 监听器",
      groupId: "R:dify",
      answerEngine: "dify",
      difyAppId: "stock-flow",
      difyTimeoutSeconds: 480,
    });

    expect(legacy.answerEngine).toBe("mcp");
    expect(legacy.runtimeVersion).toBe("v2");
    expect(legacy.difyAppId).toBe("");
    expect(legacy.tuning.difyTimeoutSeconds).toBe(300);
    expect(dify.answerEngine).toBe("dify");
    expect(dify.difyAppId).toBe("stock-flow");
    expect(dify.tuning.difyTimeoutSeconds).toBe(480);
  });

  it("validates operator ranges without requiring a tool selection", () => {
    expect(tuningValidationErrors({
      ...defaultListenerDraft().tuning,
      maxConcurrency: 0,
      mcpTimeoutSeconds: 1801,
      maxAgentRounds: 201,
    })).toEqual([
      "同时检索问题数必须在 1–20 之间。",
      "单个问题 MCP 最长等待必须在 60–1800 秒之间。",
      "Agent 最大查询轮数必须在 2–200 之间。",
    ]);
  });

  it("validates the independent tool-call budget through 200 attempts", () => {
    expect(tuningValidationErrors({
      ...defaultListenerDraft().tuning,
      maxToolCalls: 0,
    })).toContain("Agent 最大工具调用数必须在 1–200 之间。");
    expect(tuningValidationErrors({
      ...defaultListenerDraft().tuning,
      maxAgentRounds: 200,
      maxToolCalls: 200,
    })).toEqual([]);
  });
});

describe("explicit secrets and pending actions", () => {
  it("normalizes Dify apps without accepting an API key from the public wire shape", () => {
    const app = difyAppFromWire({
      id: "stock-flow",
      name: "库存 Chatflow",
      enabled: true,
      baseUrl: "http://dify.internal/v1",
      inputs: { region: "cn" },
      apiKey: "must-not-surface",
      secrets: { apiKeyConfigured: true, fingerprint: "sha256-fingerprint" },
      connectionTestCurrent: true,
    });

    expect(app.secrets).toEqual({
      apiKeyConfigured: true,
      fingerprint: "sha256-fingerprint",
    });
    expect(app).not.toHaveProperty("apiKey");
  });

  it("validates the complete listener save response before reading it", () => {
    expect(listenerSaveResultFromWire({
      revision: 6,
      listener: { id: "listener-1", revision: 6, name: "Support", groupId: "room" },
    })).toEqual({
      revision: 6,
      listener: { id: "listener-1", revision: 6, name: "Support", groupId: "room" },
    });
    expect(() => listenerSaveResultFromWire(null)).toThrow("后台保存响应格式无效");
    expect(() => listenerSaveResultFromWire([])).toThrow("后台保存响应格式无效");
    expect(() => listenerSaveResultFromWire({ revision: 6 })).toThrow("没有返回监听器状态");
    expect(() => listenerSaveResultFromWire({ listener: { id: "listener-1" }, revision: "bad" }))
      .toThrow("返回的配置版本无效");
    expect(() => listenerSaveResultFromWire({ listener: { id: "listener-1" }, revision: "6" }))
      .toThrow("返回的配置版本无效");
    expect(() => listenerSaveResultFromWire({ listener: { id: "listener-1" }, revision: false }))
      .toThrow("返回的配置版本无效");
  });

  it("keeps, replaces, or clears MCP secrets without inventing a value", () => {
    expect(parseRecordSecretEdit("keep", "ignored")).toEqual({ mode: "keep" });
    expect(parseRecordSecretEdit("clear", "ignored")).toEqual({ mode: "clear" });
    expect(parseRecordSecretEdit("replace", '{"Authorization":"Bearer token"}')).toEqual({
      mode: "replace",
      value: { Authorization: "Bearer token" },
    });
  });

  it("adds the required acknowledgement only to a plain-text mention", () => {
    expect(buildWorkActionBody("work.send", "work-1", 4)).toEqual({
      kind: "work.send",
      workId: "work-1",
      expectedVersion: 4,
    });
    expect(buildWorkActionBody("work.send_plain_at", "work-1", 4)).toEqual({
      kind: "work.send_plain_at",
      workId: "work-1",
      expectedVersion: 4,
      acknowledgement: "PLAIN_AT_IS_NOT_A_TRUE_MENTION",
    });
    expect(buildWorkActionBody("work.send", "work-1", 4, true)).toEqual({
      kind: "work.send",
      workId: "work-1",
      expectedVersion: 4,
      confirmedNotDelivered: true,
    });
    expect(buildWorkActionBody("work.discard", "work-1", 4, true)).toEqual({
      kind: "work.discard",
      workId: "work-1",
      expectedVersion: 4,
    });
    expect(buildWorkActionBody("work.retry_images", "work-1", 4)).toEqual({
      kind: "work.retry_images",
      workId: "work-1",
      expectedVersion: 4,
    });
  });
});

describe("runtime page visibility contract", () => {
  it("does not report the runtime online before a snapshot is available", () => {
    expect(runtimeAvailabilityCopy(undefined).label).toBe("运行状态未知");
    expect(runtimeAvailabilityCopy(true).label).toBe("运行模块在线");
    expect(runtimeAvailabilityCopy(false).label).toBe("运行模块已停止");

    const source = readGroupReplySources();
    expect(source.indexOf("else setSnapshot({});")).toBeLessThan(source.indexOf('if (listenerResult.status === "rejected")'));
  });

  it("loads only listener groups and Dify apps for the editor", () => {
    const source = readGroupReplySources();
    expect(source).toContain("bridge.listGroups()");
    expect(source).toContain('kind: "dify.list"');
    expect(source).not.toContain('kind: "mcp.list"');
    expect(source).not.toContain('kind: "mcp.catalog"');
  });

  it("lets listener loading finish without waiting for slow editor options or event storms", () => {
    const source = readGroupReplySources();
    const runtimeLoadStart = source.indexOf("const load = useCallback");
    const editorOptionsStart = source.indexOf("const loadEditorOptions = useCallback");
    const effectsStart = source.indexOf("\n  useEffect", editorOptionsStart);

    expect(runtimeLoadStart).toBeGreaterThan(-1);
    expect(editorOptionsStart).toBeGreaterThan(runtimeLoadStart);
    expect(effectsStart).toBeGreaterThan(editorOptionsStart);

    const runtimeLoad = source.slice(runtimeLoadStart, editorOptionsStart);
    const editorOptionsLoad = source.slice(editorOptionsStart, effectsStart);
    expect(runtimeLoad).not.toContain("bridge.listGroups()");
    expect(runtimeLoad).not.toContain('kind: "dify.list"');
    expect(editorOptionsLoad).toContain("bridge.listGroups()");
    expect(editorOptionsLoad).toContain('kind: "dify.list"');
    expect(editorOptionsLoad).not.toContain('kind: "mcp.catalog"');
    expect(source).toContain("createGroupReplyEventRefreshScheduler");
    expect(source).toContain("{ startPaused: true }");
    expect(source).toContain("scheduler.resume()");
    expect(source).toContain("foregroundLoadSequence");
    expect(source).toContain("foregroundSequence === foregroundLoadSequence.current");
    expect(source).toContain("setListeners((current) => applyGroupReplyRuntimeEvent(current, event))");
    expect(source).toContain("scheduler.notify(event)");
    const subscription = source.indexOf("await bridge.onReplyRuntimeEvent(handleEvent)");
    const initialLoad = source.indexOf("await load();", subscription);
    expect(subscription).toBeGreaterThan(-1);
    expect(initialLoad).toBeGreaterThan(subscription);
  });

  it("marks stale MCP catalogs as unavailable instead of schema-confirmed", () => {
    const source = readFileSync(fileURLToPath(new URL("./McpServicesPage.tsx", import.meta.url)), "utf8");
    expect(source).toContain("catalogUnavailable || schemaLabel(tool)");
    expect(source).toContain("请恢复服务并重新测试、发现工具");
    expect(source).toContain("CATALOG_REFRESH_FAILED");
  });

  it("refreshes persisted MCP catalog state after both successful and failed connection tests", () => {
    const source = readFileSync(fileURLToPath(new URL("./McpServicesPage.tsx", import.meta.url)), "utf8");
    const testStart = source.indexOf("const test = async");
    const testEnd = source.indexOf("\n  return (", testStart);
    const testBlock = source.slice(testStart, testEnd);

    expect(testStart).toBeGreaterThan(-1);
    expect(testEnd).toBeGreaterThan(testStart);
    expect(testBlock.match(/await load\(true\);/g)).toHaveLength(2);
  });

  it("labels MCP health as a recent test result and keeps a cached catalog usable after a transient failure", () => {
    const source = readFileSync(fileURLToPath(new URL("./McpServicesPage.tsx", import.meta.url)), "utf8");
    expect(source).toContain("mcpLastTestCopy({");
    expect(source).toContain("最近测试成功");
    expect(source).toContain("最近测试结果只描述测试时刻，不代表服务持续在线或掉线。");
    expect(source).toContain("mcpCatalogSnapshotCopy(catalogEntry?.updatedAt, tools.length)");
    expect(source).toContain("status.tone === \"warning\"");
    expect(source).not.toContain('label: "连接正常"');
    expect(source).not.toContain('label: "连接失败"');
  });

  it("keeps the five easily confused timing controls visible with distinct names", () => {
    const source = readGroupReplySources();
    for (const label of [
      "等待连续补充时长",
      "留给群友回答的时间",
      "个人上下文保留时间",
      "同时检索问题数",
      "单个问题 MCP 最长等待",
    ]) expect(source).toContain(label);
    expect(source).toContain("只决定后台多久检查一次企微本地新消息；不是从发送到开始 MCP 检索的总耗时。");
    expect(source).toContain("仅在收集期内生效；同一人的有效补充会从最后一条重新计时。");
  });

  it("switches listener answer-engine controls without mixing MCP and Dify requirements", () => {
    const source = readGroupReplySources();
    expect(source).toContain("回答引擎");
    expect(source).toContain("MCP + 提示词");
    expect(source).toContain("Dify Chatflow");
    expect(source).toContain('draft.answerEngine === "mcp"');
    expect(source).toContain('draft.answerEngine === "dify"');
    expect(source).toContain("单个问题 Dify 最长等待");
    expect(source).toContain("请选择一个 Dify Chatflow 应用");
    expect(source).toContain('kind: "dify.list"');
  });

  it("exposes the Dify workbench with explicit secret update semantics", () => {
    const page = readFileSync(fileURLToPath(new URL("./DifyAppsPage.tsx", import.meta.url)), "utf8");
    const app = readFileSync(fileURLToPath(new URL("../App.tsx", import.meta.url)), "utf8");
    const navigation = readFileSync(fileURLToPath(new URL("../navigation.ts", import.meta.url)), "utf8");
    for (const kind of ["dify.list", "dify.save", "dify.test", "dify.delete"]) {
      expect(page).toContain(`kind: "${kind}"`);
    }
    expect(page).toContain('type SecretMode = "keep" | "replace" | "clear"');
    expect(page).toContain("HTTP 会以明文传输 API Key 与对话内容");
    expect(page).toContain("实际以上传接口为准");
    expect(app).toContain('page === "strategy-dify"');
    expect(navigation).toContain('id: "strategy-dify"');
  });

  it("keeps an open work detail current and renders timing, image, and folded-history context", () => {
    const source = readGroupReplySources();
    const styles = readFileSync(fileURLToPath(new URL("../index.css", import.meta.url)), "utf8");
    expect(source).toContain("createSelectedWorkDetailRefresher");
    expect(source).toContain("await refreshOpenWorkDetail();");
    expect(source).toContain("workStageTimeline(detail).map");
    expect(source).toContain("imageStatusCopy(detail.imageStatus, detail.imageCount, detail.imageAvailableCount, detail.imageUnavailableCount)");
    expect(source).toContain("已折叠 {detail.duplicateCount} 条重复记录");
    expect(styles).toContain(".work-detail-context .image-state.is-resolving { border-color: hsl(var(--primary)");
    const imageWarningRules = styles.split("\n")
      .filter((line) => line.includes("image-state") && line.includes("var(--warning)"));
    expect(imageWarningRules.join("\n")).not.toContain("is-resolving");
    expect(source).toContain("图片未参与分析，系统已停止自动回复");
    expect(source).toContain('workAction(detail, "work.retry_images")');
    expect(source).toContain('workAction(detail, "work.continue_without_images")');
    expect(source).toContain('<details className="work-evidence">');
    expect(styles).toContain("width: min(920px,100%)");
    expect(styles).toContain(".reply-page .work-detail-section p { font-size: 16px");
  });

  it("requires an explicit unknown-delivery retry and sends every work mutation with revision CAS", () => {
    const source = readGroupReplySources();
    expect(source).toContain("确认群内未出现，重新发送");
    expect(source).toContain("buildWorkActionBody(kind, item.id, item.version, retryingUnknownDelivery)");
    expect(source).toMatch(/buildWorkActionBody\(kind, item\.id, item\.version, retryingUnknownDelivery\),\s*runtimeRevision/);
  });

  it("queries active, pending and history as separate server-side pages", () => {
    const source = readGroupReplySources();
    expect(source).toContain('bucket: "active"');
    expect(source).toContain('bucket: "pending"');
    expect(source).toContain('bucket: "history"');
    expect(source).toContain("page: activePage.page");
    expect(source).toContain("page: pendingPage.page");
    expect(source).toContain("page: historyPage.page");
    expect(source).toContain('view === "active"');
    expect(source).toContain("function WorkPagination");
  });

  it("removes legacy runtime and per-tool grant controls from the listener editor", () => {
    const source = readGroupReplySources();
    expect(source).not.toContain("tool-grant-list");
    expect(source).not.toContain("toolStrategies");
    expect(source).not.toContain("selectedTools");
    expect(source).not.toContain("runtime-version-choice");
    expect(source).toContain("Agent V2");
    for (const step of ["监听对象", "引擎与 Runtime", "发送方式", "预算"]) {
      expect(source).toContain(step);
    }
  });

  it("keeps the four listener metrics equally sized with aligned values and equal card heights", () => {
    const css = readFileSync(
      fileURLToPath(new URL("../groupreply.css", import.meta.url)),
      "utf8",
    );
    expect(css).toContain("grid-template-columns: repeat(4, minmax(0, 1fr))");
    expect(css).toContain("font-variant-numeric: tabular-nums");
    expect(css).toContain("align-items: baseline");
    expect(css).toContain("height: 100%");
    expect(css).toContain(".listener-grid > .listener-card");

    const source = readGroupReplySources();
    expect(source).toContain("listener-fact-value");
    expect(source).toContain("listener-fact-label");
    expect(source).not.toContain("LEGACY");
    expect(source).toContain('import "../groupreply.css"');
  });

  it("keeps Agent execution logging opt-in and exposes the local log directory", () => {
    const settings = readFileSync(fileURLToPath(new URL("./SettingsPage.tsx", import.meta.url)), "utf8");
    const listener = readGroupReplySources();
    expect(settings).toContain("Agent 执行日志");
    expect(settings).toContain("记录群回复 Agent 执行日志");
    expect(settings).toContain("agent_execution_logging: checked");
    expect(settings).toContain("bridge.openAgentLogDirectory()");
    expect(settings).toContain("默认关闭");
    expect(listener).toContain('max={200}');
    expect(listener).toContain("最高 200 轮");
  });

  it("renders Agent V2 policy controls, safety copy and runtime filters", () => {
    const listener = readGroupReplySources();
    const mcp = readFileSync(fileURLToPath(new URL("./McpServicesPage.tsx", import.meta.url)), "utf8");
    for (const copy of [
      "Agent V2",
      "Agent 最大工具调用",
      "全部引擎",
      "全部停止原因",
      "全部恢复状态",
      "Agent 执行时间线",
      "导出诊断",
      "重新检索",
    ]) expect(listener).toContain(copy);
    expect(listener).toContain('kind: "work.export_diagnostics"');
    expect(listener).toContain('"work.retry"');
    expect(listener).toContain("manual_retry_scheduled");
    // v4.2.2: MCP tool grants are automatic; the page must not expose manual
    // read-only confirmation or call the removed backend policy command.
    expect(mcp).not.toContain('kind: "mcp.tool_policy"');
    expect(mcp).not.toContain("确认为只读");
    expect(mcp).not.toContain("撤销人工确认");
    expect(mcp).not.toContain("待确认");
    expect(mcp).toContain("未声明");
    expect(mcp).toContain("破坏性");
    expect(mcp).toContain("仅只读工具会自动提供给 Agent，写/破坏性/未声明工具不会暴露");
  });

  it("shows model capability probes, bounded log retention and responsive workbench layouts", () => {
    const settings = readFileSync(fileURLToPath(new URL("./SettingsPage.tsx", import.meta.url)), "utf8");
    const styles = readFileSync(fileURLToPath(new URL("../index.css", import.meta.url)), "utf8");
    for (const copy of [
      "结构化输出",
      "原生 Tool Calling",
      "图片输入",
      "上下文读取",
      "检测全部能力",
      "清理详细日志",
      "轻量时间线始终保留 30 天",
    ]) expect(settings).toContain(copy);
    expect(settings).toContain('kind: "model.test_capabilities"');
    expect(settings).toContain('kind: "diagnostics.clear"');
    expect(settings).toContain('acknowledgement: "DELETE_AGENT_LOGS"');
    expect(styles).toContain("@media (min-width: 1760px)");
    expect(styles).toContain("@media (max-width: 1080px)");
    expect(styles).toContain("@media (max-width: 680px)");
    expect(styles).toContain("@media (prefers-reduced-motion: reduce)");
    expect(styles).toContain(".work-detail-sticky-actions { position: sticky");
    expect(styles).toContain(".work-filter-bar { display: grid");
  });

  it("wires the Jev settings card to its status query, synthetic probe and tab refresh", () => {
    const settings = readFileSync(fileURLToPath(new URL("./SettingsPage.tsx", import.meta.url)), "utf8");
    for (const copy of [
      "Jev 语义判断（可选）",
      "数据外发告知",
      "检测连接",
      "尚未检测",
      "影子（shadow）",
      "辅助（assist）",
      "关闭时不发送任何 Jev 请求",
    ]) {
      expect(settings).toContain(copy);
    }
    expect(settings).toContain('kind: "jev.status"');
    expect(settings).toContain('kind: "jev.test_connection"');
    // The key is edited through a password field and never rendered in clear.
    expect(settings).toContain('type="password"');

    // Regression guard: opening the models tab must load the *existing* Jev
    // status too. Without this the card always reads "尚未检测" on a fresh
    // mount, even when a passing health check is already recorded.
    const effectStart = settings.indexOf('if (tab === "models")');
    const effectEnd = settings.indexOf("}, [", effectStart);
    expect(effectStart).toBeGreaterThan(-1);
    const effect = settings.slice(effectStart, effectEnd);
    expect(effect).toContain("refreshModelCapabilities(true)");
    expect(effect).toContain("refreshJevStatus(true)");
  });
});

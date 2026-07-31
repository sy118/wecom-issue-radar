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
  toolGrantSelectionKey,
  toggleToolSelection,
  tuningValidationErrors,
} from "../lib/replyRuntimeUi";
import { listenerFromWire, listenerSaveResultFromWire } from "./GroupReplyPage";

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

  it("serializes the selected group's exact tool grants and timing without unit ambiguity", () => {
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
      toolGrants: [{ serverId: "kb", toolName: "search", schemaSha256: "sha-42" }],
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
        toolGrants: [{ serverId: "kb", toolName: "search", schemaSha256: "sha-42" }],
        systemPrompt: draft.systemPrompt,
        pollIntervalSeconds: 5,
        sameSenderMergeSeconds: 20,
        humanReplyWaitSeconds: 120,
        sessionTimeoutSeconds: 1800,
        maxConcurrency: 4,
        mcpTimeoutSeconds: 900,
        autoSend: false,
      },
      secretPatch: { webhookUrl: { mode: "keep" } },
    });
  });

  it("validates operator ranges and toggles one schema-pinned tool without widening access", () => {
    expect(tuningValidationErrors({
      ...defaultListenerDraft().tuning,
      maxConcurrency: 0,
      mcpTimeoutSeconds: 1801,
    })).toEqual(["同时检索问题数必须在 1–20 之间。", "单个问题 MCP 最长等待必须在 60–1800 秒之间。"]);

    expect(toggleToolSelection(["kb:search:sha-1"], "crm:lookup:sha-2")).toEqual([
      "kb:search:sha-1",
      "crm:lookup:sha-2",
    ]);
    expect(toggleToolSelection(["kb:search:sha-1"], "kb:search:sha-1")).toEqual([]);

    const oldSchema = toolGrantSelectionKey("kb", "search", "sha-old");
    const newSchema = toolGrantSelectionKey("kb", "search", "sha-new");
    expect(toggleToolSelection([oldSchema], newSchema)).toEqual([newSchema]);
  });
});

describe("explicit secrets and pending actions", () => {
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
  });
});

describe("runtime page visibility contract", () => {
  it("does not report the runtime online before a snapshot is available", () => {
    expect(runtimeAvailabilityCopy(undefined).label).toBe("运行状态未知");
    expect(runtimeAvailabilityCopy(true).label).toBe("运行模块在线");
    expect(runtimeAvailabilityCopy(false).label).toBe("运行模块已停止");

    const source = readFileSync(fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url)), "utf8");
    expect(source.indexOf("else setSnapshot({});")).toBeLessThan(source.indexOf('if (listenerResult.status === "rejected")'));
  });

  it("filters unhealthy catalogs before building selectable tool grants", () => {
    const source = readFileSync(fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url)), "utf8");
    expect(source).toContain("mcpCatalogAllowsGrants(serverMap.get(entry.serverId), entry)");
    expect(source).toContain('if (mcpResult.status === "rejected" || catalogResult.status === "rejected") setTools([]);');
  });

  it("lets listener loading finish without waiting for slow editor options or event storms", () => {
    const source = readFileSync(fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url)), "utf8");
    const runtimeLoadStart = source.indexOf("const load = useCallback");
    const editorOptionsStart = source.indexOf("const loadEditorOptions = useCallback");
    const effectsStart = source.indexOf("\n  useEffect", editorOptionsStart);

    expect(runtimeLoadStart).toBeGreaterThan(-1);
    expect(editorOptionsStart).toBeGreaterThan(runtimeLoadStart);
    expect(effectsStart).toBeGreaterThan(editorOptionsStart);

    const runtimeLoad = source.slice(runtimeLoadStart, editorOptionsStart);
    const editorOptionsLoad = source.slice(editorOptionsStart, effectsStart);
    expect(runtimeLoad).not.toContain("bridge.listGroups()");
    expect(runtimeLoad).not.toContain('kind: "mcp.catalog"');
    expect(editorOptionsLoad).toContain("bridge.listGroups()");
    expect(editorOptionsLoad).toContain('kind: "mcp.catalog"');
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
    const source = readFileSync(fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url)), "utf8");
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

  it("keeps an open work detail current and renders timing, image, and folded-history context", () => {
    const source = readFileSync(fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url)), "utf8");
    expect(source).toContain("createSelectedWorkDetailRefresher");
    expect(source).toContain("await refreshOpenWorkDetail();");
    expect(source).toContain("workStageTimeline(detail).map");
    expect(source).toContain("imageStatusCopy(detail.imageStatus, detail.imageCount)");
    expect(source).toContain("已折叠 {detail.duplicateCount} 条重复记录");
  });

  it("requires an explicit unknown-delivery retry and sends every work mutation with revision CAS", () => {
    const source = readFileSync(fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url)), "utf8");
    expect(source).toContain("确认群内未出现，重新发送");
    expect(source).toContain("buildWorkActionBody(kind, item.id, item.version, retryingUnknownDelivery)");
    expect(source).toMatch(/buildWorkActionBody\(kind, item\.id, item\.version, retryingUnknownDelivery\),\s*runtimeRevision/);
  });

  it("queries active, pending and history as separate server-side pages", () => {
    const source = readFileSync(fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url)), "utf8");
    expect(source).toContain('bucket: "active"');
    expect(source).toContain('bucket: "pending"');
    expect(source).toContain('bucket: "history"');
    expect(source).toContain("page: activePage.page");
    expect(source).toContain("page: pendingPage.page");
    expect(source).toContain("page: historyPage.page");
    expect(source).toContain('view === "active"');
    expect(source).toContain("function WorkPagination");
  });

  it("shows missing grants as removable and submits only exact current catalog matches", () => {
    const source = readFileSync(fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url)), "utf8");
    expect(source).toContain("unavailableToolKeys.map");
    expect(source).toContain("原授权已失效");
    expect(source).toContain("点击移除");
    expect(source).toMatch(/const selectedToolGrants = useMemo\(\(\) => draft\.selectedTools\.flatMap/);
    expect(source).not.toContain('return toolGrantFromSelectionKey(key) ?? { serverId: "", toolName: "", schemaSha256: "" }');
  });
});

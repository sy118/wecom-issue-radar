import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { bridge } from "./bridge";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));

const invokeMock = vi.mocked(invoke);
const listenMock = vi.mocked(listen);

beforeEach(() => {
  invokeMock.mockReset();
  listenMock.mockReset();
});

describe("bridge ReplyRuntime seam", () => {
  it("keeps commands, queries, and events on the versioned runtime protocol", async () => {
    invokeMock.mockResolvedValue({ ok: true });
    listenMock.mockResolvedValue(() => undefined);
    const command = {
      protocolVersion: 1 as const,
      commandId: "cmd-1",
      body: { kind: "mcp.delete" as const, serverId: "mcp-1" },
    };
    const query = { protocolVersion: 1 as const, body: { kind: "mcp.list" as const } };
    const handler = vi.fn();

    await bridge.replyRuntimeExecute(command);
    await bridge.replyRuntimeQuery(query);
    await bridge.onReplyRuntimeEvent(handler);

    expect(invokeMock).toHaveBeenNthCalledWith(1, "reply_runtime_execute", { command });
    expect(invokeMock).toHaveBeenNthCalledWith(2, "reply_runtime_query", { query });
    expect(listenMock).toHaveBeenCalledWith("reply-runtime-event", expect.any(Function));

    const eventHandler = listenMock.mock.calls[0]?.[1];
    eventHandler?.({
      payload: {
        type: "event",
        seq: 7,
        event: { kind: "work.updated", workId: "work-1" },
      },
    } as never);
    expect(handler).toHaveBeenCalledWith({ kind: "work.updated", workId: "work-1", seq: 7 });
  });
});

describe("bridge.syncSmartSheet", () => {
  it("keeps manual sync strict while forwarding the selected template", async () => {
    invokeMock.mockResolvedValue({ synced: 3 });

    await bridge.syncSmartSheet(
      "D:/exports/team",
      "2026-07-24",
      "incident_sheet",
      false,
      "revision-42",
      "D:/exports/team/issues.json",
      "document-9",
    );

    expect(invokeMock).toHaveBeenCalledWith("sync_smart_sheet", {
      payload: {
        dayDir: "D:/exports/team",
        date: "2026-07-24",
        templateId: "incident_sheet",
        uploadImages: false,
        expectedTemplateRevision: "revision-42",
        definitionPath: "D:/exports/team/issues.json",
        expectedDocumentRevision: "document-9",
      },
    });
    expect(invokeMock.mock.calls[0]?.[1]).not.toHaveProperty("payload.allowMissingImages");
  });

  it("refreshes a Smart Sheet preview without writing external data", async () => {
    invokeMock.mockResolvedValue({
      pending: 2,
      already_synced: 0,
      template_revision: "revision-42",
      document_revision: "document-9",
    });

    await bridge.previewSmartSheet(
      "D:/exports/team",
      "2026-07-24",
      "incident_sheet",
      "D:/exports/team/issues.json",
    );

    expect(invokeMock).toHaveBeenCalledWith("preview_smart_sheet", {
      payload: {
        dayDir: "D:/exports/team",
        date: "2026-07-24",
        templateId: "incident_sheet",
        definitionPath: "D:/exports/team/issues.json",
      },
    });
  });

  it("lists and atomically clears persisted schedule confirmations", async () => {
    invokeMock.mockResolvedValue([]);

    await bridge.listPendingSmartSheetSyncs();
    await bridge.clearPendingSmartSheetSyncs(["daily:1", "daily:2"]);

    expect(invokeMock).toHaveBeenNthCalledWith(1, "list_pending_smart_sheet_syncs");
    expect(invokeMock).toHaveBeenNthCalledWith(2, "clear_pending_smart_sheet_syncs", {
      pendingIds: ["daily:1", "daily:2"],
    });
  });
});

describe("bridge config transfer", () => {
  it("forwards backup and import paths to the native configuration store", async () => {
    invokeMock.mockResolvedValue(undefined);

    await bridge.exportConfigBackup("D:/backup/issue-radar.json");
    await bridge.importConfigBackup("D:/backup/issue-radar.json");

    expect(invokeMock).toHaveBeenNthCalledWith(1, "export_config_backup", {
      path: "D:/backup/issue-radar.json",
    });
    expect(invokeMock).toHaveBeenNthCalledWith(2, "import_config_backup", {
      path: "D:/backup/issue-radar.json",
    });
  });
});

describe("bridge local diagnostics", () => {
  it("opens the Agent log directory through the native shell", async () => {
    invokeMock.mockResolvedValue("C:/Users/test/.wecom-issue-radar/logs/agent");

    const path = await bridge.openAgentLogDirectory();

    expect(path).toBe("C:/Users/test/.wecom-issue-radar/logs/agent");
    expect(invokeMock).toHaveBeenCalledWith("open_agent_log_directory");
  });
});

describe("bridge schedule history", () => {
  it("forwards one-based pagination and an optional task filter", async () => {
    invokeMock.mockResolvedValue({ items: [], page: 2, pageSize: 10, total: 0, totalPages: 0 });

    await bridge.listScheduleExecutionHistory(2, 10, "daily");
    await bridge.listScheduleExecutionHistory(1, 10);

    expect(invokeMock).toHaveBeenNthCalledWith(1, "list_schedule_execution_history", {
      page: 2,
      pageSize: 10,
      scheduleId: "daily",
    });
    expect(invokeMock).toHaveBeenNthCalledWith(2, "list_schedule_execution_history", {
      page: 1,
      pageSize: 10,
      scheduleId: null,
    });
  });
});

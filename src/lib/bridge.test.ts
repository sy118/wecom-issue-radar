import { invoke } from "@tauri-apps/api/core";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { bridge } from "./bridge";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const invokeMock = vi.mocked(invoke);

beforeEach(() => {
  invokeMock.mockReset();
});

describe("bridge.syncSmartSheet", () => {
  it("forwards the selected template and image-upload preference", async () => {
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

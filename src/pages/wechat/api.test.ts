import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/bridge", () => ({
  bridge: {
    wechatAction: vi.fn(),
    wechatRuntimeStatus: vi.fn(),
  },
}));

import { bridge } from "../../lib/bridge";
import { bootstrapWechat, loadWechatRuntimeStatus } from "./api";

const action = vi.mocked(bridge.wechatAction);
const runtimeStatus = vi.mocked(bridge.wechatRuntimeStatus);

describe("wechat api bootstrap", () => {
  beforeEach(() => {
    action.mockReset();
    runtimeStatus.mockReset();
  });

  it("normalizes the single bootstrap payload", async () => {
    action.mockResolvedValue({
      detect: {
        found: true,
        running: true,
        dbDir: "E:/wechat/db_storage",
        accounts: [],
        candidates: [],
        selected: true,
        ready: true,
      },
      workspace: { projects: [] },
      settings: { settings: { contextMessageLimit: 50 } },
      providers: { providers: [], mcpServers: [] },
    });

    const data = await bootstrapWechat();

    expect(action).toHaveBeenCalledWith("bootstrap", {});
    expect(data.detect.found).toBe(true);
    expect(data.projects).toEqual([]);
    expect(data.settings.contextMessageLimit).toBe(50);
  });

  it("propagates bootstrap failure so the caller can fall back", async () => {
    action.mockRejectedValue(new Error("worker unavailable"));
    await expect(bootstrapWechat()).rejects.toThrow();
  });

  it("reports unavailable when the bridge does not expose runtime status", async () => {
    const status = await loadWechatRuntimeStatus();
    expect(status.mode).toBe("unavailable");
    expect(status.started).toBe(false);
  });

  it("reads resident runtime status when available", async () => {
    runtimeStatus.mockResolvedValue({
      mode: "resident",
      started: true,
      pid: 123,
      restarts: 0,
      inFlight: 1,
      lastError: "",
      startupMs: 42,
    });

    const status = await loadWechatRuntimeStatus();
    expect(status.mode).toBe("resident");
    expect(status.startupMs).toBe(42);
  });
});

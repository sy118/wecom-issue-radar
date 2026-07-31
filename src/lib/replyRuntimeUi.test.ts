import { describe, expect, it, vi } from "vitest";
import {
  automaticDeliveryBlockers,
  defaultListenerDraft,
  executeListenerSave,
  secretEditForExistingValue,
  secretEditForNewValue,
} from "./replyRuntimeUi";

describe("ReplyRuntime operator defaults", () => {
  it("starts safely with review mode and practical timing for long MCP retrieval", () => {
    const draft = defaultListenerDraft();

    expect(draft.deliveryMode).toBe("review");
    expect(draft.enabled).toBe(false);
    expect(draft.tuning).toEqual({
      pollIntervalSeconds: 5,
      sameSenderMergeSeconds: 20,
      humanReplyWaitSeconds: 120,
      sessionTimeoutSeconds: 1800,
      maxConcurrency: 4,
      mcpTimeoutSeconds: 900,
    });
  });

  it("blocks automatic sending until the selected group's webhook is visibly confirmed", () => {
    expect(automaticDeliveryBlockers({
      deliveryMode: "automatic",
      webhookConfigured: true,
      webhookVerified: false,
      selectedToolCount: 1,
    })).toEqual(["请先发送测试消息，并确认它出现在当前选择的群聊中。"]);

    expect(automaticDeliveryBlockers({
      deliveryMode: "automatic",
      webhookConfigured: true,
      webhookVerified: true,
      selectedToolCount: 1,
    })).toEqual([]);
  });
});

describe("secret editing", () => {
  it("never replaces an existing secret unless the operator explicitly chooses to", () => {
    expect(secretEditForExistingValue()).toEqual({ mode: "keep" });
    expect(secretEditForNewValue("Bearer new-token")).toEqual({
      mode: "replace",
      value: "Bearer new-token",
    });
  });
});

describe("listener save concurrency", () => {
  it("refreshes a new listener revision and retries one concurrent revision conflict", async () => {
    const revisions = [5, 6];
    const readRevision = vi.fn(async () => revisions.shift() ?? 6);
    const execute = vi.fn(async (command: { commandId: string; expectedRevision?: number }) => {
      if (command.expectedRevision === 5) throw { code: "REVISION_CONFLICT" };
      return { revision: 7 };
    });

    await expect(executeListenerSave({
      draft: defaultListenerDraft(),
      body: { kind: "listener.save" },
      readRevision,
      execute,
    })).resolves.toEqual({ revision: 7 });
    expect(readRevision).toHaveBeenCalledTimes(2);
    expect(execute.mock.calls.map(([command]) => command.expectedRevision)).toEqual([5, 6]);
    expect(new Set(execute.mock.calls.map(([command]) => command.commandId)).size).toBe(2);
  });

  it("stops after one retry when a new listener keeps conflicting", async () => {
    const conflict = { code: "REVISION_CONFLICT" };
    const readRevision = vi.fn(async () => 5);
    const execute = vi.fn(async (_command: { expectedRevision?: number }) => { throw conflict; });

    await expect(executeListenerSave({
      draft: defaultListenerDraft(),
      body: { kind: "listener.save" },
      readRevision,
      execute,
    })).rejects.toBe(conflict);
    expect(readRevision).toHaveBeenCalledTimes(2);
    expect(execute).toHaveBeenCalledTimes(2);
  });

  it("does not rebase an existing listener over a concurrent edit", async () => {
    const conflict = { code: "REVISION_CONFLICT" };
    const readRevision = vi.fn(async () => 9);
    const execute = vi.fn(async (_command: { expectedRevision?: number }) => { throw conflict; });

    await expect(executeListenerSave({
      draft: { ...defaultListenerDraft(), id: "listener-1", revision: 3 },
      body: { kind: "listener.save" },
      readRevision,
      execute,
    })).rejects.toBe(conflict);
    expect(readRevision).not.toHaveBeenCalled();
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute.mock.calls[0]?.[0].expectedRevision).toBe(3);
  });
});

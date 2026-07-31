import { describe, expect, it } from "vitest";
import {
  automaticDeliveryBlockers,
  defaultListenerDraft,
  listenerSaveExpectedRevision,
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
  it("uses the current runtime revision for a new draft opened before loading completed", () => {
    expect(listenerSaveExpectedRevision({ id: undefined, revision: 0 }, 5)).toBe(5);
  });

  it("keeps the editor revision for an existing listener so concurrent edits still conflict", () => {
    expect(listenerSaveExpectedRevision({ id: "listener-1", revision: 3 }, 5)).toBe(3);
  });
});

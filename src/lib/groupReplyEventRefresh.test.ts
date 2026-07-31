import { describe, expect, it, vi } from "vitest";
import type { ReplyListenerSummary } from "../types";
import {
  applyGroupReplyRuntimeEvent,
  createGroupReplyEventRefreshScheduler,
  createSelectedWorkDetailRefresher,
} from "./groupReplyEventRefresh";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("group reply runtime event refresh", () => {
  it("only applies the newest response for the work detail that is still open", async () => {
    let resolveFirst!: (value: string) => void;
    let resolveSecond!: (value: string) => void;
    const first = new Promise<string>((resolve) => { resolveFirst = resolve; });
    const second = new Promise<string>((resolve) => { resolveSecond = resolve; });
    const applied: string[] = [];
    const refresher = createSelectedWorkDetailRefresher<string>((value) => applied.push(value));

    refresher.select("work-a");
    const staleRequest = refresher.refresh("work-a", () => first);
    refresher.select("work-b");
    const currentRequest = refresher.refresh("work-b", () => second);

    resolveSecond("new detail");
    await currentRequest;
    resolveFirst("stale detail");
    await staleRequest;

    expect(applied).toEqual(["new detail"]);
    expect(refresher.currentId()).toBe("work-b");
  });

  it("surfaces a listener poll failure locally without a backend reload", () => {
    const listener = {
      id: "listener-1",
      health: "monitoring",
      healthMessage: "运行条件已就绪",
    } as ReplyListenerSummary;

    const updated = applyGroupReplyRuntimeEvent([listener], {
      kind: "listener.poll_failed",
      listenerId: "listener-1",
      message: "cannot read group messages",
    });

    expect(updated[0]).toMatchObject({
      id: "listener-1",
      health: "error",
      healthMessage: "cannot read group messages",
    });
  });

  it("reuses listener state when the same poll failure repeats", () => {
    const listeners = [{
      id: "listener-1",
      health: "error",
      healthMessage: "cannot read group messages",
    }] as ReplyListenerSummary[];

    const updated = applyGroupReplyRuntimeEvent(listeners, {
      kind: "listener.poll_failed",
      listenerId: "listener-1",
      message: "cannot read group messages",
    });

    expect(updated).toBe(listeners);
  });

  it("coalesces repeated listener poll failures into one backend reload", async () => {
    vi.useFakeTimers();
    const refresh = vi.fn(async () => undefined);
    const scheduler = createGroupReplyEventRefreshScheduler(refresh);

    for (let index = 0; index < 100; index += 1) {
      scheduler.notify({ kind: "listener.poll_failed", listenerId: "listener-1" });
    }
    await vi.advanceTimersByTimeAsync(180);

    expect(refresh).toHaveBeenCalledTimes(1);
    scheduler.dispose();
    vi.useRealTimers();
  });

  it("bounds an event storm to one in-flight refresh and one trailing refresh", async () => {
    vi.useFakeTimers();
    const first = deferred();
    const second = deferred();
    const refresh = vi.fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const scheduler = createGroupReplyEventRefreshScheduler(refresh);

    for (let index = 0; index < 100; index += 1) {
      scheduler.notify({ kind: "runtime.activity" });
    }
    await vi.advanceTimersByTimeAsync(180);
    expect(refresh).toHaveBeenCalledTimes(1);

    for (let index = 0; index < 100; index += 1) {
      scheduler.notify({ kind: "runtime.activity" });
    }
    await vi.advanceTimersByTimeAsync(1_000);
    expect(refresh).toHaveBeenCalledTimes(1);

    first.resolve();
    await vi.advanceTimersByTimeAsync(180);
    expect(refresh).toHaveBeenCalledTimes(2);

    second.resolve();
    scheduler.dispose();
    vi.useRealTimers();
  });

  it("holds event refreshes until the initial page load has settled", async () => {
    vi.useFakeTimers();
    const refresh = vi.fn(async () => undefined);
    const scheduler = createGroupReplyEventRefreshScheduler(refresh, { startPaused: true });

    scheduler.notify({ kind: "runtime.activity" });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(refresh).not.toHaveBeenCalled();

    scheduler.resume();
    await vi.advanceTimersByTimeAsync(180);
    expect(refresh).toHaveBeenCalledTimes(1);

    scheduler.dispose();
    vi.useRealTimers();
  });
});

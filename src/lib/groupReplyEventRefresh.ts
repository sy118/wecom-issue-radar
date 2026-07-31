import type { ReplyListenerSummary, ReplyRuntimeEvent } from "../types";

const DEFAULT_EVENT_REFRESH_DELAY_MS = 180;

export interface GroupReplyEventRefreshScheduler {
  notify: (event: ReplyRuntimeEvent) => void;
  resume: () => void;
  dispose: () => void;
}

export interface GroupReplyEventRefreshOptions {
  delayMs?: number;
  startPaused?: boolean;
}

export interface SelectedWorkDetailRefresher<T> {
  select: (workId: string) => void;
  clear: () => void;
  currentId: () => string | null;
  refresh: (workId: string, request: () => Promise<T>) => Promise<boolean>;
}

export function createSelectedWorkDetailRefresher<T>(
  apply: (value: T) => void,
): SelectedWorkDetailRefresher<T> {
  let selectedWorkId: string | null = null;
  let requestSequence = 0;

  return {
    select(workId) {
      selectedWorkId = workId;
      requestSequence += 1;
    },
    clear() {
      selectedWorkId = null;
      requestSequence += 1;
    },
    currentId() {
      return selectedWorkId;
    },
    async refresh(workId, request) {
      const sequence = ++requestSequence;
      const value = await request();
      if (sequence !== requestSequence || selectedWorkId !== workId) return false;
      apply(value);
      return true;
    },
  };
}

export function applyGroupReplyRuntimeEvent(
  listeners: ReplyListenerSummary[],
  event: ReplyRuntimeEvent,
): ReplyListenerSummary[] {
  if (event.kind !== "listener.poll_failed" || typeof event.listenerId !== "string") {
    return listeners;
  }
  const message = typeof event.message === "string" && event.message.trim()
    ? event.message.trim()
    : "读取群消息失败，请检查企微登录状态和本机消息数据库。";
  let changed = false;
  const next = listeners.map((listener) => {
    if (listener.id !== event.listenerId
      || (listener.health === "error" && listener.healthMessage === message)) {
      return listener;
    }
    changed = true;
    return { ...listener, health: "error" as const, healthMessage: message };
  });
  return changed ? next : listeners;
}

export function createGroupReplyEventRefreshScheduler(
  refresh: () => Promise<void>,
  options: GroupReplyEventRefreshOptions = {},
): GroupReplyEventRefreshScheduler {
  const delayMs = options.delayMs ?? DEFAULT_EVENT_REFRESH_DELAY_MS;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  let paused = options.startPaused ?? false;
  let refreshRunning = false;
  let refreshQueued = false;

  const scheduleQueuedRefresh = () => {
    if (disposed || paused || refreshRunning || !refreshQueued || timer !== undefined) return;
    timer = setTimeout(() => {
      timer = undefined;
      if (disposed || !refreshQueued) return;
      refreshQueued = false;
      refreshRunning = true;
      void refresh()
        .catch(() => undefined)
        .finally(() => {
          refreshRunning = false;
          scheduleQueuedRefresh();
        });
    }, delayMs);
  };

  return {
    notify(event) {
      if (disposed) return;
      refreshQueued = true;
      scheduleQueuedRefresh();
    },
    resume() {
      if (disposed) return;
      paused = false;
      scheduleQueuedRefresh();
    },
    dispose() {
      disposed = true;
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
      refreshQueued = false;
    },
  };
}

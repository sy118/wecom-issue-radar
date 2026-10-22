/**
 * Data/loading layer for the group reply page: listener list, work buckets,
 * runtime snapshot, editor option catalogs, live event subscription and the
 * open work-detail refresher. UI state (tabs, editor draft, filters) stays in
 * the page shell.
 */

import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { toast } from "sonner";

import { bridge } from "../../lib/bridge";
import { toUserErrorMessage } from "../../lib/errors";
import {
  applyGroupReplyRuntimeEvent,
  createGroupReplyEventRefreshScheduler,
  createSelectedWorkDetailRefresher,
} from "../../lib/groupReplyEventRefresh";
import { createQuery } from "../../lib/replyRuntimeUi";
import type {
  DifyAppSummary,
  GroupInfo,
  ReplyListenerSummary,
  ReplyRuntimeEvent,
  ReplyRuntimeSnapshot,
  ReplyWorkItem,
} from "../../types";
import {
  WORK_PAGE_SIZE,
  collection,
  difyAppOptionFromWire,
  listenerFromWire,
  snapshotFromWire,
  workFromWire,
  type WorkFilters,
  type WorkPageState,
} from "./shared";

export interface GroupReplyData {
  listeners: ReplyListenerSummary[];
  activePage: WorkPageState;
  pendingPage: WorkPageState;
  historyPage: WorkPageState;
  setActivePage: Dispatch<SetStateAction<WorkPageState>>;
  setPendingPage: Dispatch<SetStateAction<WorkPageState>>;
  setHistoryPage: Dispatch<SetStateAction<WorkPageState>>;
  groups: GroupInfo[];
  difyApps: DifyAppSummary[];
  snapshot: ReplyRuntimeSnapshot;
  runtimeRevision: number;
  setRuntimeRevision: Dispatch<SetStateAction<number>>;
  loading: boolean;
  detail: ReplyWorkItem | null;
  load: (quiet?: boolean) => Promise<void>;
  loadEditorOptions: () => Promise<void>;
  openWorkDetail: (item: ReplyWorkItem) => Promise<void>;
  closeWorkDetail: () => void;
  refreshOpenWorkDetail: () => Promise<void>;
  activeItems: ReplyWorkItem[];
  pendingItems: ReplyWorkItem[];
  historyItems: ReplyWorkItem[];
}

export function useGroupReplyData(workFilters: WorkFilters): GroupReplyData {
  const [listeners, setListeners] = useState<ReplyListenerSummary[]>([]);
  const [works, setWorks] = useState<ReplyWorkItem[]>([]);
  const [activePage, setActivePage] = useState<WorkPageState>({ page: 1, total: 0 });
  const [pendingPage, setPendingPage] = useState<WorkPageState>({ page: 1, total: 0 });
  const [historyPage, setHistoryPage] = useState<WorkPageState>({ page: 1, total: 0 });
  const [groups, setGroups] = useState<GroupInfo[]>([]);
  const [difyApps, setDifyApps] = useState<DifyAppSummary[]>([]);
  const [snapshot, setSnapshot] = useState<ReplyRuntimeSnapshot>({});
  const [runtimeRevision, setRuntimeRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<ReplyWorkItem | null>(null);
  const loadSequence = useRef(0);
  const foregroundLoadSequence = useRef(0);
  const editorOptionsLoadSequence = useRef(0);
  const detailRefresher = useRef<ReturnType<typeof createSelectedWorkDetailRefresher<ReplyWorkItem>> | null>(null);
  if (!detailRefresher.current) {
    detailRefresher.current = createSelectedWorkDetailRefresher((nextDetail) => setDetail(nextDetail));
  }

  const queryWorkDetail = useCallback(async (workId: string): Promise<ReplyWorkItem> => {
    const result = await bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "work.detail", workId }));
    const raw = (result.item ?? result.work ?? result) as Record<string, unknown>;
    return workFromWire(raw);
  }, []);

  const refreshOpenWorkDetail = useCallback(async () => {
    const selectedId = detailRefresher.current?.currentId();
    if (!selectedId) return;
    try {
      await detailRefresher.current?.refresh(selectedId, () => queryWorkDetail(selectedId));
    } catch {
      // Keep the last usable detail while the runtime moves between states.
    }
  }, [queryWorkDetail]);

  const closeWorkDetail = useCallback(() => {
    detailRefresher.current?.clear();
    setDetail(null);
  }, []);

  const load = useCallback(async (quiet = false) => {
    const sequence = ++loadSequence.current;
    const foregroundSequence = quiet ? undefined : ++foregroundLoadSequence.current;
    if (!quiet) setLoading(true);
    try {
      const filterQuery = {
        ...(workFilters.search.trim() ? { search: workFilters.search.trim() } : {}),
        ...(workFilters.answerEngine ? { answerEngine: workFilters.answerEngine } : {}),
        ...(workFilters.stage ? { status: workFilters.stage } : {}),
        ...(workFilters.stopReason ? { stopReason: workFilters.stopReason } : {}),
        ...(workFilters.recovery ? { recovery: workFilters.recovery } : {}),
      };
      const [listenerResult, activeResult, pendingResult, historyResult, snapshotResult] = await Promise.allSettled([
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "listener.list" })),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({
          kind: "work.list",
          bucket: "active",
          page: activePage.page,
          limit: WORK_PAGE_SIZE,
          ...filterQuery,
        })),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({
          kind: "work.list",
          bucket: "pending",
          page: pendingPage.page,
          limit: WORK_PAGE_SIZE,
          ...filterQuery,
        })),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({
          kind: "work.list",
          bucket: "history",
          page: historyPage.page,
          limit: WORK_PAGE_SIZE,
          ...filterQuery,
        })),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "runtime.snapshot" })),
      ]);
      if (sequence !== loadSequence.current) return;
      if (snapshotResult.status === "fulfilled") setSnapshot(snapshotFromWire(snapshotResult.value));
      else setSnapshot({});
      if (listenerResult.status === "rejected") throw listenerResult.reason;
      if (activeResult.status === "rejected") throw activeResult.reason;
      if (pendingResult.status === "rejected") throw pendingResult.reason;
      if (historyResult.status === "rejected") throw historyResult.reason;
      const rawListeners = collection<Record<string, unknown>>(listenerResult.value, ["listeners", "items"]);
      const nextListeners = rawListeners.map(listenerFromWire);
      setListeners(nextListeners);
      setRuntimeRevision(Number(listenerResult.value.revision ?? 0));
      const listenerMap = new Map(nextListeners.map((listener) => [listener.id, listener]));
      const activeItems = collection<Record<string, unknown>>(activeResult.value, ["items", "works"]);
      const pendingItems = collection<Record<string, unknown>>(pendingResult.value, ["items", "works"]);
      const historyItems = collection<Record<string, unknown>>(historyResult.value, ["items", "works"]);
      const workItems = [...activeItems, ...pendingItems, ...historyItems];
      setWorks(workItems.map(workFromWire).map((item) => ({
          ...item,
          listenerName: item.listenerName || listenerMap.get(item.listenerId)?.name,
          answerEngine: item.answerEngine ?? listenerMap.get(item.listenerId)?.answerEngine,
          groupName: item.groupName === item.groupId
            ? listenerMap.get(item.listenerId)?.groupName ?? item.groupName
            : item.groupName,
      })));
      const nextActiveTotal = Number(activeResult.value.total ?? activeItems.length);
      const nextPendingTotal = Number(pendingResult.value.total ?? pendingItems.length);
      const nextHistoryTotal = Number(historyResult.value.total ?? historyItems.length);
      const activeLastPage = Math.max(1, Math.ceil(nextActiveTotal / WORK_PAGE_SIZE));
      const pendingLastPage = Math.max(1, Math.ceil(nextPendingTotal / WORK_PAGE_SIZE));
      const historyLastPage = Math.max(1, Math.ceil(nextHistoryTotal / WORK_PAGE_SIZE));
      setActivePage((current) => ({ page: Math.min(current.page, activeLastPage), total: nextActiveTotal }));
      setPendingPage((current) => ({ page: Math.min(current.page, pendingLastPage), total: nextPendingTotal }));
      setHistoryPage((current) => ({ page: Math.min(current.page, historyLastPage), total: nextHistoryTotal }));
      await refreshOpenWorkDetail();
    } catch (error) {
      toast.error("无法读取群监听配置", { description: toUserErrorMessage(error, "请确认后台运行模块已经启动。") });
    } finally {
      if (foregroundSequence === foregroundLoadSequence.current) setLoading(false);
    }
  }, [activePage.page, historyPage.page, pendingPage.page, refreshOpenWorkDetail, workFilters]);

  const loadEditorOptions = useCallback(async () => {
    const sequence = ++editorOptionsLoadSequence.current;
    const [groupResult, difyResult] = await Promise.allSettled([
      bridge.listGroups(),
      bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "dify.list" })),
    ]);
    if (sequence !== editorOptionsLoadSequence.current) return;
    if (groupResult.status === "fulfilled") setGroups(groupResult.value.groups ?? []);
    setDifyApps(difyResult.status === "fulfilled"
      ? collection<Record<string, unknown>>(difyResult.value, ["apps", "items"]).map(difyAppOptionFromWire)
      : []);
  }, []);

  useEffect(() => {
    const scheduler = createGroupReplyEventRefreshScheduler(
      () => load(true),
      { startPaused: true },
    );
    let disposed = false;
    let unlisten: (() => void) | undefined;
    const handleEvent = (event: ReplyRuntimeEvent) => {
      setListeners((current) => applyGroupReplyRuntimeEvent(current, event));
      scheduler.notify(event);
    };
    void (async () => {
      try {
        const dispose = await bridge.onReplyRuntimeEvent(handleEvent);
        if (disposed) {
          dispose();
          return;
        }
        unlisten = dispose;
      } catch {
        // Runtime queries still provide a useful page if event subscription fails.
      }
      if (disposed) return;
      await load();
      if (!disposed) scheduler.resume();
    })();
    return () => {
      disposed = true;
      scheduler.dispose();
      unlisten?.();
    };
  }, [load]);

  useEffect(() => {
    void loadEditorOptions();
  }, [loadEditorOptions]);

  useEffect(() => {
    if (!detail || (detail.status !== "waiting" && detail.status !== "working")) return;
    const interval = window.setInterval(() => {
      void refreshOpenWorkDetail();
    }, 1500);
    return () => window.clearInterval(interval);
  }, [detail?.id, detail?.status, refreshOpenWorkDetail]);

  const openWorkDetail = async (item: ReplyWorkItem) => {
    detailRefresher.current?.select(item.id);
    setDetail(item);
    try {
      await detailRefresher.current?.refresh(item.id, () => queryWorkDetail(item.id));
    } catch {
      // The list item remains useful if a detail refresh races with a state transition.
    }
  };

  const activeItems = works.filter((item) => item.status === "waiting" || item.status === "working");
  const pendingItems = works.filter((item) => item.status === "pending");
  const historyItems = works.filter((item) => ["sent", "closed", "failed"].includes(item.status));

  return {
    listeners,
    activePage,
    pendingPage,
    historyPage,
    setActivePage,
    setPendingPage,
    setHistoryPage,
    groups,
    difyApps,
    snapshot,
    runtimeRevision,
    setRuntimeRevision,
    loading,
    detail,
    load,
    loadEditorOptions,
    openWorkDetail,
    closeWorkDetail,
    refreshOpenWorkDetail,
    activeItems,
    pendingItems,
    historyItems,
  };
}

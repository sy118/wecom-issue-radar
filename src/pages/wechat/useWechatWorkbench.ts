/**
 * State and data orchestration for the personal WeChat workbench.
 *
 * Keeps all bridge calls, polling and draft lifecycle in one hook so the page
 * stays a composition layer. Every failure is surfaced as inline state or a
 * toast; nothing here blocks the rest of the page.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import * as api from "./api";
import {
  canLoadMore,
  draftIsStale,
  emptyDraft,
  incrementUnread,
  mergeProjectMessages,
  nextMessageLimit,
  normalizeDraft,
  selectedProject as pickSelected,
  unreadForProject,
} from "./logic";
import {
  DEFAULT_CONTEXT_MESSAGE_LIMIT,
  DEFAULT_REPLY_STYLES,
  POLL_INTERVAL_MS,
  emptyLlmConfig,
  type ReplyStyle,
  type WechatChat,
  type WechatDraft,
  type WechatDetectResult,
  type WechatExportResult,
  type WechatLlmConfig,
  type WechatMcpServer,
  type WechatMediaPreview,
  type WechatMessage,
  type WechatPollProject,
  type WechatProject,
  type WechatProvider,
  type WechatScanResult,
  type WechatSettings,
} from "./types";

export interface ProjectEditorState {
  mode: "new" | "edit";
  project?: WechatProject;
}

const errorText = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);

// A conversation can contain dozens of images. Each preview crosses the
// Electron/Python bridge and starts a worker process, so cap the fan-out;
// otherwise entering a chat can spawn a burst of sidecars and stall the UI.
const MEDIA_CONCURRENCY = 2;
let mediaActive = 0;
const mediaWaiters: Array<() => void> = [];

async function runWithMediaSlot<T>(task: () => Promise<T>): Promise<T> {
  if (mediaActive >= MEDIA_CONCURRENCY) {
    await new Promise<void>((resolve) => mediaWaiters.push(resolve));
  }
  mediaActive += 1;
  try {
    return await task();
  } finally {
    mediaActive = Math.max(0, mediaActive - 1);
    mediaWaiters.shift()?.();
  }
}

const withProjectId = (draft: WechatDraft, projectId: string): WechatDraft => ({
  ...emptyDraft(projectId),
  ...draft,
  projectId,
});

export function useWechatWorkbench({ active = true }: { active?: boolean } = {}) {
  const backendAvailable = useMemo(() => api.wechatBackendAvailable(), []);
  const [detect, setDetect] = useState<WechatDetectResult | null>(null);
  const [detectLoading, setDetectLoading] = useState(false);
  const [detectError, setDetectError] = useState("");
  const [scan, setScan] = useState<WechatScanResult | null>(null);
  const [scanning, setScanning] = useState(false);
  const [chats, setChats] = useState<WechatChat[]>([]);
  const [chatsLoading, setChatsLoading] = useState(false);
  const [chatsError, setChatsError] = useState("");
  const [projects, setProjects] = useState<WechatProject[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectsError, setProjectsError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [messagesByProject, setMessagesByProject] = useState<Record<string, WechatMessage[]>>({});
  const [messageLimits, setMessageLimits] = useState<Record<string, number>>({});
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [messagesError, setMessagesError] = useState("");
  const [drafts, setDrafts] = useState<Record<string, WechatDraft>>({});
  const [generating, setGenerating] = useState(false);
  const [rewriting, setRewriting] = useState("");
  const [savingDraft, setSavingDraft] = useState(false);
  const [filling, setFilling] = useState(false);
  const [providers, setProviders] = useState<WechatProvider[]>([]);
  const [llm, setLlm] = useState<WechatLlmConfig>(emptyLlmConfig);
  const [mcpServers, setMcpServers] = useState<WechatMcpServer[]>([]);
  const [settings, setSettings] = useState<WechatSettings | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [configError, setConfigError] = useState("");
  const [savingSettings, setSavingSettings] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [savingProject, setSavingProject] = useState(false);
  const [selectingAccount, setSelectingAccount] = useState("");
  const [unread, setUnread] = useState<Record<string, number>>({});
  const [editor, setEditor] = useState<ProjectEditorState | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const selectedIdRef = useRef("");
  const mediaCache = useRef(new Map<string, Promise<WechatMediaPreview>>());

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  const selected = useMemo(
    () => pickSelected(projects, selectedId),
    [projects, selectedId],
  );
  const selectedMessages = useMemo(
    () => (selected ? messagesByProject[selected.id] ?? [] : []),
    [messagesByProject, selected],
  );
  const selectedDraft = selected ? drafts[selected.id] : undefined;
  const messageLimit = selected
    ? messageLimits[selected.id] ?? DEFAULT_CONTEXT_MESSAGE_LIMIT
    : DEFAULT_CONTEXT_MESSAGE_LIMIT;
  const hasMoreMessages = canLoadMore(selectedMessages, messageLimit);
  const staleDraft = draftIsStale(selectedDraft, selectedMessages);
  const replyStyles = useMemo<ReplyStyle[]>(
    () => (settings?.replyStyles?.length ? settings.replyStyles : DEFAULT_REPLY_STYLES),
    [settings],
  );

  // Pick the first project whenever the current selection disappears.
  useEffect(() => {
    if (!projects.length) {
      if (selectedId) setSelectedId("");
      return;
    }
    if (!projects.some((project) => project.id === selectedId)) {
      setSelectedId(projects[0].id);
    }
  }, [projects, selectedId]);

  const refreshProjects = useCallback(async (): Promise<void> => {
    setProjectsLoading(true);
    setProjectsError("");
    try {
      const next = await api.listWechatProjects();
      setProjects(next);
      // `projects` responses include a compact draft summary; seed the draft
      // panel so an existing draft appears before the first poll/generation.
      setDrafts((current) => {
        let map = current;
        for (const project of next) {
          const summary = normalizeDraft((project as Record<string, unknown>).draft);
          if (!summary) continue;
          map = { ...map, [project.id]: withProjectId(summary, project.id) };
        }
        return map;
      });
    } catch (error) {
      setProjectsError(errorText(error));
    } finally {
      setProjectsLoading(false);
    }
  }, []);

  const loadChats = useCallback(async (): Promise<void> => {
    if (!backendAvailable) {
      setChatsError("个人微信后端尚未接入，暂时无法读取会话。");
      return;
    }
    setChatsLoading(true);
    setChatsError("");
    try {
      setChats(await api.listWechatChats());
    } catch (error) {
      setChatsError(errorText(error));
    } finally {
      setChatsLoading(false);
    }
  }, [backendAvailable]);

  const detectWechat = useCallback(async (force = false): Promise<void> => {
    if (!backendAvailable) {
      setDetectError("个人微信后端尚未接入，暂时无法检测微信数据目录。");
      return;
    }
    setDetectLoading(true);
    setDetectError("");
    try {
      const result = await api.detectWechat(force);
      setDetect(result);
    } catch (error) {
      setDetectError(errorText(error));
    } finally {
      setDetectLoading(false);
    }
  }, [backendAvailable]);

  const selectAccount = useCallback(
    async (dbDir: string): Promise<void> => {
      if (!dbDir) return;
      setSelectingAccount(dbDir);
      try {
        await api.selectWechatAccount(dbDir);
        toast.success("已切换微信数据目录");
        await Promise.allSettled([detectWechat(), loadChats(), refreshProjects()]);
      } catch (error) {
        toast.error("切换微信账号失败", { description: errorText(error) });
      } finally {
        setSelectingAccount("");
      }
    },
    [detectWechat, loadChats, refreshProjects],
  );

  const scanKeys = useCallback(async (dbDir?: string): Promise<void> => {
    if (!backendAvailable) {
      toast.error("个人微信后端尚未接入，暂时无法扫描密钥。");
      return;
    }
    setScanning(true);
    try {
      const result = await api.scanWechatKeys(dbDir);
      setScan(result);
      if (result.ok) {
        toast.success(result.message || "微信数据库密钥已验证");
        void loadChats();
        void refreshProjects();
      } else {
        toast.error(result.message || "密钥扫描未通过，请确认微信正在运行。");
      }
    } catch (error) {
      toast.error("扫描微信密钥失败", { description: errorText(error) });
    } finally {
      setScanning(false);
    }
  }, [backendAvailable, loadChats, refreshProjects]);

  const loadMessages = useCallback(
    async (projectId: string, limit: number, mode: "replace" | "append" = "replace"): Promise<void> => {
      if (!projectId) return;
      if (mode === "append") setLoadingMore(true);
      else setMessagesLoading(true);
      setMessagesError("");
      try {
        const incoming = await api.listWechatMessages(projectId, limit);
        setMessagesByProject((current) =>
          mode === "append"
            ? mergeProjectMessages(current, projectId, incoming).map
            : { ...current, [projectId]: incoming },
        );
        setMessageLimits((current) => ({ ...current, [projectId]: limit }));
      } catch (error) {
        setMessagesError(errorText(error));
      } finally {
        if (mode === "append") setLoadingMore(false);
        else setMessagesLoading(false);
      }
    },
    [],
  );

  // First message load when a project becomes selected.
  useEffect(() => {
    if (!selected) return;
    if (messagesByProject[selected.id]) return;
    void loadMessages(selected.id, DEFAULT_CONTEXT_MESSAGE_LIMIT);
  }, [selected, messagesByProject, loadMessages]);

  const selectProject = useCallback((projectId: string) => {
    setSelectedId(projectId);
    setUnread((current) => (current[projectId] ? { ...current, [projectId]: 0 } : current));
  }, []);

  const loadMoreMessages = useCallback(async (): Promise<void> => {
    if (!selected || loadingMore) return;
    await loadMessages(selected.id, nextMessageLimit(messageLimit), "append");
  }, [loadingMore, loadMessages, messageLimit, selected]);

  const refreshSelectedMessages = useCallback(async (): Promise<void> => {
    if (!selected) return;
    await loadMessages(selected.id, messageLimit);
  }, [loadMessages, messageLimit, selected]);

  const refreshAll = useCallback(async (): Promise<void> => {
    await Promise.allSettled([
      refreshProjects(),
      loadChats(),
      detectWechat(),
      selected ? loadMessages(selected.id, messageLimit) : Promise.resolve(),
    ]);
  }, [detectWechat, loadChats, loadMessages, messageLimit, refreshProjects, selected]);

  const generateDraft = useCallback(async (): Promise<void> => {
    if (!selected) return;
    setGenerating(true);
    try {
      const draft = await api.generateWechatDraft(selected.id);
      if (draft?.content) {
        setDrafts((current) => ({ ...current, [selected.id]: withProjectId(draft, selected.id) }));
        toast.success("草稿已生成");
      } else if (draft) {
        setDrafts((current) => ({ ...current, [selected.id]: withProjectId(draft, selected.id) }));
        toast.info("草稿生成请求已提交，完成后会自动更新。");
      } else {
        toast.info("草稿生成请求已提交，完成后会自动更新。");
      }
    } catch (error) {
      toast.error("生成草稿失败", { description: errorText(error) });
      setDrafts((current) => ({
        ...current,
        [selected.id]: {
          ...(current[selected.id] ?? emptyDraft(selected.id)),
          status: "error",
          errorMessage: errorText(error),
        },
      }));
    } finally {
      setGenerating(false);
    }
  }, [selected]);

  const rewriteDraft = useCallback(
    async (style: ReplyStyle): Promise<void> => {
      if (!selected) return;
      setRewriting(style.id);
      try {
        const draft = await api.rewriteWechatDraft(selected.id, {
          styleId: style.id,
          instruction: style.instruction,
        });
        if (draft) {
          setDrafts((current) => ({ ...current, [selected.id]: withProjectId(draft, selected.id) }));
        }
        toast.success(`已按「${style.label}」改写草稿`);
      } catch (error) {
        toast.error("改写草稿失败", { description: errorText(error) });
      } finally {
        setRewriting("");
      }
    },
    [selected],
  );

  const saveDraft = useCallback(
    async (content: string): Promise<void> => {
      if (!selected) return;
      if (content === (drafts[selected.id]?.content ?? "")) return;
      setSavingDraft(true);
      try {
        const draft = await api.saveWechatDraft(selected.id, content);
        setDrafts((current) => ({
          ...current,
          [selected.id]: draft
            ? withProjectId(draft, selected.id)
            : { ...(current[selected.id] ?? emptyDraft(selected.id)), content, status: "ready", stale: false },
        }));
      } catch (error) {
        toast.error("保存草稿失败", { description: errorText(error) });
      } finally {
        setSavingDraft(false);
      }
    },
    [drafts, selected],
  );

  const copyDraft = useCallback(async (content: string): Promise<void> => {
    if (!content.trim()) return;
    try {
      await navigator.clipboard.writeText(content);
      toast.success("草稿已复制");
    } catch {
      toast.error("无法写入剪贴板，请在草稿框中手动全选复制。");
    }
  }, []);

  const fillDraft = useCallback(
    async (content: string): Promise<void> => {
      if (!selected || !content.trim()) return;
      setFilling(true);
      toast.info("请在 8 秒内切回目标微信聊天，并点击空白输入框。", { duration: 8000 });
      try {
        const result = await api.fillWechatInput(selected.chatTitle || selected.name, content);
        if (!result.filled) {
          throw new Error("未能填入微信输入框，请确认聊天窗口和光标位置。");
        }
        toast.success(`已填入「${result.chatTitle}」输入框，共 ${result.characters} 字，请手动确认发送。`);
      } catch (error) {
        toast.warning(errorText(error), {
          duration: 7000,
          action: { label: "复制草稿", onClick: () => void copyDraft(content) },
        });
      } finally {
        setFilling(false);
      }
    },
    [copyDraft, selected],
  );

  const openNewProject = useCallback(() => {
    setEditor({ mode: "new" });
    void loadChats();
  }, [loadChats]);

  const openEditProject = useCallback((project: WechatProject) => {
    setEditor({ mode: "edit", project });
  }, []);

  const closeEditor = useCallback(() => setEditor(null), []);

  const saveProject = useCallback(
    async (project: WechatProject): Promise<void> => {
      setSavingProject(true);
      try {
        const saved = await api.saveWechatProject(project);
        setProjects((current) => {
          const exists = current.some((item) => item.id === saved.id);
          return exists
            ? current.map((item) => (item.id === saved.id ? saved : item))
            : [saved, ...current];
        });
        setSelectedId(saved.id);
        setEditor(null);
        toast.success(project.id === saved.id && project.revision > 0 ? "项目已更新" : "项目已保存");
      } catch (error) {
        toast.error("保存项目失败", { description: errorText(error) });
        throw error;
      } finally {
        setSavingProject(false);
      }
    },
    [],
  );

  const deleteProject = useCallback(
    async (project: WechatProject): Promise<void> => {
      try {
        await api.deleteWechatProject(project.id);
        setProjects((current) => current.filter((item) => item.id !== project.id));
        setDrafts((current) => {
          const next = { ...current };
          delete next[project.id];
          return next;
        });
        setMessagesByProject((current) => {
          const next = { ...current };
          delete next[project.id];
          return next;
        });
        setEditor(null);
        toast.success("项目已删除");
      } catch (error) {
        toast.error("删除项目失败", { description: errorText(error) });
      }
    },
    [],
  );

  const exportProject = useCallback(
    async (projectId: string, start?: string, end?: string): Promise<WechatExportResult> => {
      setExporting(true);
      try {
        const result = await api.exportWechatProject(projectId, start, end);
        toast.success("导出完成", {
          description: result.dir || (result.files.length ? `${result.files.length} 个文件` : undefined),
        });
        return result;
      } catch (error) {
        toast.error("导出聊天记录失败", { description: errorText(error) });
        throw error;
      } finally {
        setExporting(false);
      }
    },
    [],
  );

  const loadConfig = useCallback(async (): Promise<void> => {
    if (!backendAvailable) return;
    setConfigLoading(true);
    setConfigError("");
    try {
      const [providerResult, settingsResult] = await Promise.allSettled([
        api.loadWechatProviders(),
        api.loadWechatSettings(),
      ]);
      if (providerResult.status === "fulfilled") {
        setProviders(providerResult.value.providers);
        setLlm(providerResult.value.llm);
        setMcpServers(providerResult.value.mcpServers);
      }
      if (settingsResult.status === "fulfilled") setSettings(settingsResult.value);
      if (providerResult.status === "rejected" && settingsResult.status === "rejected") {
        setConfigError(errorText(providerResult.reason));
      }
    } finally {
      setConfigLoading(false);
    }
  }, [backendAvailable]);

  const saveSettings = useCallback(
    async (next: WechatSettings): Promise<void> => {
      setSavingSettings(true);
      try {
        const saved = await api.saveWechatSettings(next);
        setSettings(saved);
        toast.success("运行偏好已保存");
      } catch (error) {
        toast.error("保存运行偏好失败", { description: errorText(error) });
        throw error;
      } finally {
        setSavingSettings(false);
      }
    },
    [],
  );

  const loadMedia = useCallback(
    (projectId: string, messageId: string): Promise<WechatMediaPreview> => {
      const key = `${projectId}:${messageId}`;
      const cached = mediaCache.current.get(key);
      if (cached) return cached;
      const pending = runWithMediaSlot(() => api.previewWechatMedia(projectId, messageId)).catch((error) => {
        mediaCache.current.delete(key);
        throw error;
      });
      if (mediaCache.current.size >= 64) {
        const oldest = mediaCache.current.keys().next().value;
        if (typeof oldest === "string") mediaCache.current.delete(oldest);
      }
      mediaCache.current.set(key, pending);
      return pending;
    },
    [],
  );

  // Live "monitoring" feel: merge poll results every 10s while the page is visible.
  const applyPoll = useCallback((entries: WechatPollProject[]): void => {
    if (!entries.length) return;
    setMessagesByProject((current) => {
      let map = current;
      for (const entry of entries) {
        if (!entry.newMessages.length) continue;
        map = mergeProjectMessages(map, entry.projectId, entry.newMessages).map;
      }
      return map;
    });
    setDrafts((current) => {
      let next = current;
      for (const entry of entries) {
        if (!entry.draft) continue;
        next = { ...next, [entry.projectId]: withProjectId(entry.draft, entry.projectId) };
      }
      return next;
    });
    setUnread((current) => {
      const additions: Record<string, number> = {};
      for (const entry of entries) {
        if (entry.projectId === selectedIdRef.current) continue;
        if (entry.unreadDelta > 0) {
          additions[entry.projectId] = (additions[entry.projectId] ?? 0) + entry.unreadDelta;
        }
      }
      return incrementUnread(current, additions);
    });
  }, []);

  useEffect(() => {
    if (!backendAvailable || !active) return;
    let disposed = false;
    const tick = async (): Promise<void> => {
      if (disposed || document.visibilityState !== "visible") return;
      try {
        const entries = await api.pollWechat();
        if (!disposed) applyPoll(entries);
      } catch {
        // Continuous monitoring is best-effort; never interrupt the user with a toast.
      }
    };
    const timer = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    // Let the first message batch render before competing with a poll worker.
    const firstTick = window.setTimeout(() => void tick(), 3_000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      window.clearTimeout(firstTick);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [applyPoll, backendAvailable, active]);

  const loadBootstrap = useCallback(async (quiet = false): Promise<void> => {
    if (!backendAvailable) {
      setProjectsLoading(false);
      setProjectsError("个人微信后端尚未接入，暂时无法读取会话。");
      return;
    }
    if (!quiet) {
      setProjectsLoading(true);
      setDetectLoading(true);
      setConfigLoading(true);
      setProjectsError("");
      setDetectError("");
      setConfigError("");
    }
    try {
      const data = await api.bootstrapWechat();
      setDetect(data.detect);
      setProjects(data.projects);
      setSettings(data.settings);
      setProviders(data.providers.providers);
      setLlm(data.providers.llm);
      setMcpServers(data.providers.mcpServers);
      setProjectsError("");
      setDetectError("");
      setConfigError("");
    } catch {
      // Older/limited bridges may not implement bootstrap yet; keep the
      // existing per-request path working as a fallback.
      await Promise.allSettled([refreshProjects(), detectWechat(), loadConfig()]);
    } finally {
      if (!quiet) {
        setProjectsLoading(false);
        setDetectLoading(false);
        setConfigLoading(false);
      }
    }
  }, [backendAvailable, detectWechat, loadConfig, refreshProjects]);

  // Initial load: one bootstrap request; falls back to the older fan-out when
  // the bridge/worker does not support it.
  useEffect(() => {
    void loadBootstrap();
  }, [loadBootstrap]);

  // The page is kept mounted by App. When it becomes visible again, refresh
  // projects/settings/detect so changes made in the config center (directory,
  // project deletion, reply styles) are reflected without a full reload.
  const wasActive = useRef(active);
  useEffect(() => {
    if (!wasActive.current && active) {
      void loadBootstrap(true);
    }
    wasActive.current = active;
  }, [active, loadBootstrap]);

  return {
    backendAvailable,
    detect,
    detectLoading,
    detectError,
    scan,
    scanning,
    chats,
    chatsLoading,
    chatsError,
    projects,
    projectsLoading,
    projectsError,
    selected,
    selectedId,
    selectedMessages,
    messagesLoading,
    loadingMore,
    messagesError,
    hasMoreMessages,
    selectedDraft,
    staleDraft,
    generating,
    rewriting,
    savingDraft,
    filling,
    providers,
    llm,
    mcpServers,
    settings,
    replyStyles,
    configLoading,
    configError,
    savingSettings,
    exporting,
    savingProject,
    selectingAccount,
    selectAccount,
    unread,
    editor,
    settingsOpen,
    exportOpen,
    setSettingsOpen,
    setExportOpen,
    refreshProjects,
    refreshAll,
    loadChats,
    detectWechat,
    scanKeys,
    selectProject,
    loadMoreMessages,
    refreshSelectedMessages,
    generateDraft,
    rewriteDraft,
    saveDraft,
    copyDraft,
    fillDraft,
    openNewProject,
    openEditProject,
    closeEditor,
    saveProject,
    deleteProject,
    exportProject,
    saveSettings,
    loadConfig,
    loadMedia,
    unreadFor: (project: WechatProject) => unreadForProject(project, chats, unread),
  };
}

export type WechatWorkbenchController = ReturnType<typeof useWechatWorkbench>;

/**
 * State and data orchestration for the personal WeChat configuration center.
 *
 * Kept separate from ``useWechatWorkbench`` so the configuration page never
 * mounts the workbench polling loop. Every write goes through the same bridge
 * actions as the workbench; failures surface as inline errors.
 */

import { useCallback, useMemo, useState } from "react";
import { toast } from "sonner";

import * as api from "./api";
import type { ProjectEditorState } from "./useWechatWorkbench";
import {
  DEFAULT_REPLY_STYLES,
  type ReplyStyle,
  type WechatChat,
  type WechatDetectResult,
  type WechatImportResult,
  type WechatMcpServer,
  type WechatProject,
  type WechatProvider,
  type WechatScanResult,
  type WechatSettings,
} from "./types";

const errorText = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);

type Navigate = (page: string) => void;

export function useWechatConfig(onNavigate?: Navigate) {
  const backendAvailable = useMemo(() => api.wechatBackendAvailable(), []);
  const [detect, setDetect] = useState<WechatDetectResult | null>(null);
  const [detectLoading, setDetectLoading] = useState(false);
  const [detectError, setDetectError] = useState("");
  const [scan, setScan] = useState<WechatScanResult | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanningDir, setScanningDir] = useState("");
  const [selecting, setSelecting] = useState("");
  const [settings, setSettings] = useState<WechatSettings | null>(null);
  const [draftSettings, setDraftSettings] = useState<WechatSettings | null>(null);
  const [providers, setProviders] = useState<WechatProvider[]>([]);
  const [mcpServers, setMcpServers] = useState<WechatMcpServer[]>([]);
  const [projects, setProjects] = useState<WechatProject[]>([]);
  const [chats, setChats] = useState<WechatChat[]>([]);
  const [chatsLoading, setChatsLoading] = useState(false);
  const [chatsError, setChatsError] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<WechatImportResult | null>(null);
  const [savingProject, setSavingProject] = useState(false);
  const [editor, setEditor] = useState<ProjectEditorState | null>(null);

  const loadDetection = useCallback(async (): Promise<void> => {
    if (!backendAvailable) {
      setDetectError("个人微信后端尚未接入，无法检测微信数据目录。");
      return;
    }
    setDetectLoading(true);
    setDetectError("");
    try {
      setDetect(await api.detectWechat());
    } catch (failure) {
      setDetectError(errorText(failure));
    } finally {
      setDetectLoading(false);
    }
  }, [backendAvailable]);

  const loadSettings = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError("");
    try {
      const [settingsResult, providerResult, projectResult] = await Promise.allSettled([
        api.loadWechatSettings(),
        api.loadWechatProviders(),
        api.listWechatProjects(),
      ]);
      if (settingsResult.status === "fulfilled") {
        setSettings(settingsResult.value);
        setDraftSettings(structuredClone(settingsResult.value));
      }
      if (providerResult.status === "fulfilled") {
        setProviders(providerResult.value.providers);
        setMcpServers(providerResult.value.mcpServers);
      }
      if (projectResult.status === "fulfilled") setProjects(projectResult.value);
      const failures = [settingsResult, providerResult, projectResult].filter(
        (item) => item.status === "rejected",
      );
      if (failures.length === 3) {
        setError(errorText((settingsResult as PromiseRejectedResult).reason));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshAll = useCallback(async (): Promise<void> => {
    await Promise.allSettled([loadDetection(), loadSettings()]);
  }, [loadDetection, loadSettings]);

  const scanKeys = useCallback(
    async (dbDir?: string): Promise<void> => {
      if (!backendAvailable) return;
      setScanning(true);
      setScanningDir(dbDir || detect?.dbDir || "");
      try {
        const result = await api.scanWechatKeys(dbDir || detect?.dbDir || undefined);
        setScan(result);
        if (result.ok) {
          toast.success(result.message || "微信数据库密钥已验证");
          await Promise.allSettled([loadDetection(), loadSettings()]);
        } else {
          toast.error(result.message || "密钥扫描未通过，请确认微信正在运行。");
        }
      } catch (failure) {
        toast.error("扫描微信密钥失败", { description: errorText(failure) });
      } finally {
        setScanning(false);
        setScanningDir("");
      }
    },
    [backendAvailable, detect?.dbDir, loadDetection, loadSettings],
  );

  const selectAccount = useCallback(
    async (dbDir: string): Promise<void> => {
      if (!backendAvailable || !dbDir) return;
      setSelecting(dbDir);
      try {
        await api.selectWechatAccount(dbDir);
        toast.success("已切换微信数据目录");
        await Promise.allSettled([loadDetection(), loadSettings()]);
      } catch (failure) {
        toast.error("切换微信账号失败", { description: errorText(failure) });
      } finally {
        setSelecting("");
      }
    },
    [backendAvailable, loadDetection, loadSettings],
  );

  const updateDraft = useCallback((patch: Partial<WechatSettings>) => {
    setDraftSettings((current) => (current ? { ...current, ...patch } : current));
  }, []);

  const updateReplyStyles = useCallback(
    (updater: (styles: ReplyStyle[]) => ReplyStyle[]) => {
      setDraftSettings((current) => {
        if (!current) return current;
        const styles = current.replyStyles?.length ? current.replyStyles : DEFAULT_REPLY_STYLES;
        return { ...current, replyStyles: updater(styles.map((style) => ({ ...style }))) };
      });
    },
    [],
  );

  const resetReplyStyles = useCallback(() => {
    updateDraft({ replyStyles: DEFAULT_REPLY_STYLES.map((style) => ({ ...style })) });
  }, [updateDraft]);

  const saveSettings = useCallback(async (): Promise<void> => {
    if (!draftSettings) return;
    setSaving(true);
    try {
      const saved = await api.saveWechatSettings(draftSettings);
      setSettings(saved);
      setDraftSettings(structuredClone(saved));
      toast.success("配置已保存");
    } catch (failure) {
      toast.error("保存配置失败", { description: errorText(failure) });
      throw failure;
    } finally {
      setSaving(false);
    }
  }, [draftSettings]);

  const importLegacy = useCallback(async (): Promise<void> => {
    if (!backendAvailable) return;
    setImporting(true);
    try {
      const result = await api.importProxytalk();
      setImportResult(result);
      if (result.imported) {
        toast.success("已导入 ProxyTalk 旧数据");
        await loadSettings();
      } else {
        toast.info(result.message || "没有找到可导入的旧数据");
      }
    } catch (failure) {
      toast.error("导入旧数据失败", { description: errorText(failure) });
    } finally {
      setImporting(false);
    }
  }, [backendAvailable, loadSettings]);

  const loadChats = useCallback(async (): Promise<void> => {
    if (!backendAvailable) return;
    setChatsLoading(true);
    setChatsError("");
    try {
      setChats(await api.listWechatChats());
    } catch (failure) {
      setChatsError(errorText(failure));
    } finally {
      setChatsLoading(false);
    }
  }, [backendAvailable]);

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
        setEditor(null);
        toast.success("项目已保存");
      } catch (failure) {
        toast.error("保存项目失败", { description: errorText(failure) });
        throw failure;
      } finally {
        setSavingProject(false);
      }
    },
    [],
  );

  const deleteProject = useCallback(async (project: WechatProject): Promise<void> => {
    if (!window.confirm(`删除项目“${project.name}”？微信原始记录不会被修改。`)) return;
    try {
      await api.deleteWechatProject(project.id);
      setProjects((current) => current.filter((item) => item.id !== project.id));
      if (editor?.project?.id === project.id) setEditor(null);
      toast.success("项目已删除");
    } catch (failure) {
      toast.error("删除项目失败", { description: errorText(failure) });
    }
  }, [editor?.project?.id]);

  const go = useCallback(
    (page: string) => {
      if (onNavigate) onNavigate(page);
    },
    [onNavigate],
  );

  return {
    backendAvailable,
    detect,
    detectLoading,
    detectError,
    scan,
    scanning,
    scanningDir,
    selecting,
    settings,
    draftSettings,
    providers,
    mcpServers,
    projects,
    chats,
    chatsLoading,
    chatsError,
    loading,
    error,
    saving,
    importing,
    importResult,
    savingProject,
    editor,
    loadDetection,
    loadSettings,
    refreshAll,
    scanKeys,
    selectAccount,
    updateDraft,
    updateReplyStyles,
    resetReplyStyles,
    saveSettings,
    importLegacy,
    loadChats,
    openNewProject,
    openEditProject,
    closeEditor,
    saveProject,
    deleteProject,
    go,
  };
}

export type WechatConfigController = ReturnType<typeof useWechatConfig>;

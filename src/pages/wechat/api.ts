/** Typed access to the personal-WeChat bridge actions. */

import { bridge as appBridge } from "../../lib/bridge";
import { toUserErrorMessage } from "../../lib/errors";
import {
  normalizeChats,
  normalizeDetect,
  normalizeDraft,
  normalizeExportResult,
  normalizeMediaPreview,
  normalizeMessages,
  normalizePoll,
  normalizeProject,
  normalizeProjects,
  normalizeProviders,
  normalizeScanResult,
  normalizeSettings,
} from "./logic";
import type {
  ReplyStyle,
  WechatChat,
  WechatConfigSnapshot,
  WechatDetectCandidate,
  WechatDetectResult,
  WechatDraft,
  WechatExportResult,
  WechatImageKeyResult,
  WechatImportResult,
  WechatMcpServer,
  WechatMediaPreview,
  WechatMessage,
  WechatPollProject,
  WechatProject,
  WechatProvider,
  WechatScanResult,
  WechatSettings,
} from "./types";

export interface WechatFillInputResult {
  filled: boolean;
  chatTitle: string;
  characters: number;
}

interface WechatBridgeApi {
  wechatAction<T = unknown>(
    action: string,
    payload?: Record<string, unknown>,
  ): Promise<T>;
  wechatFillInput?(chatTitle: string, text: string): Promise<WechatFillInputResult>;
}

function bridgeApi(): Partial<WechatBridgeApi> {
  return appBridge as unknown as Partial<WechatBridgeApi>;
}

/** True when the desktop bridge exposes the WeChat action channel. */
export function wechatBackendAvailable(): boolean {
  return typeof bridgeApi().wechatAction === "function";
}

const BACKEND_MISSING = "个人微信后端接口尚未接入（缺少 wechatAction），请更新到最新版本后重试。";

export async function callWechatAction<T = unknown>(
  action: string,
  payload?: Record<string, unknown>,
): Promise<T> {
  const api = bridgeApi();
  if (typeof api.wechatAction !== "function") {
    throw new Error(BACKEND_MISSING);
  }
  try {
    return await api.wechatAction<T>(action, payload);
  } catch (error) {
    throw new Error(toUserErrorMessage(error, "个人微信操作失败，请稍后重试。"));
  }
}

export async function fillWechatInput(
  chatTitle: string,
  text: string,
): Promise<WechatFillInputResult> {
  const api = bridgeApi();
  if (typeof api.wechatFillInput !== "function") {
    throw new Error("“填入微信”接口尚未接入，请更新到最新版本后重试。");
  }
  try {
    const result = await api.wechatFillInput(chatTitle, text);
    return {
      filled: result?.filled !== false,
      chatTitle: result?.chatTitle || chatTitle,
      characters: typeof result?.characters === "number" ? result.characters : text.length,
    };
  } catch (error) {
    throw new Error(
      toUserErrorMessage(error, "填入微信失败，请确认微信窗口已打开并重试。"),
    );
  }
}

export async function detectWechat(force = false): Promise<WechatDetectResult> {
  return normalizeDetect(await callWechatAction("detect", force ? { force: true } : {}));
}

export async function scanWechatImageKey(): Promise<WechatImageKeyResult> {
  return callWechatAction("image_key_scan");
}

export async function saveWechatImageKey(imageKey: string): Promise<WechatImageKeyResult> {
  return callWechatAction("image_key_save", { imageKey });
}

export async function scanWechatKeys(dbDir?: string): Promise<WechatScanResult> {
  return normalizeScanResult(
    await callWechatAction("scan_keys", dbDir ? { dbDir } : {}),
  );
}

export async function listWechatChats(): Promise<WechatChat[]> {
  return normalizeChats(await callWechatAction("chats"));
}

export async function listWechatProjects(): Promise<WechatProject[]> {
  return normalizeProjects(await callWechatAction("projects"));
}

export async function saveWechatProject(project: WechatProject): Promise<WechatProject> {
  const raw = await callWechatAction("project_save", { project });
  return normalizeProject(raw) ?? project;
}

export async function deleteWechatProject(projectId: string): Promise<void> {
  await callWechatAction("project_delete", { projectId });
}

export async function listWechatMessages(
  projectId: string,
  limit: number,
): Promise<WechatMessage[]> {
  return normalizeMessages(
    await callWechatAction("messages", { projectId, limit }),
    projectId,
  );
}

export async function generateWechatDraft(projectId: string): Promise<WechatDraft | null> {
  return normalizeDraft(await callWechatAction("draft_generate", { projectId }));
}

export async function rewriteWechatDraft(
  projectId: string,
  options: { styleId?: string; instruction?: string },
): Promise<WechatDraft | null> {
  const payload: Record<string, unknown> = { projectId };
  if (options.styleId) payload.styleId = options.styleId;
  if (options.instruction) payload.instruction = options.instruction;
  return normalizeDraft(await callWechatAction("draft_rewrite", payload));
}

export async function saveWechatDraft(
  projectId: string,
  content: string,
): Promise<WechatDraft | null> {
  return normalizeDraft(await callWechatAction("draft_save", { projectId, content }));
}

export async function exportWechatProject(
  projectId: string,
  start?: string,
  end?: string,
): Promise<WechatExportResult> {
  return normalizeExportResult(
    await callWechatAction("export", {
      projectId,
      ...(start ? { start } : {}),
      ...(end ? { end } : {}),
    }),
  );
}

export async function previewWechatMedia(
  projectId: string,
  messageId: string,
): Promise<WechatMediaPreview> {
  return normalizeMediaPreview(
    await callWechatAction("media_preview", { projectId, messageId }),
  );
}

export async function loadWechatProviders(): Promise<WechatConfigSnapshot> {
  return normalizeProviders(await callWechatAction("providers"));
}

export async function loadWechatSettings(): Promise<WechatSettings> {
  return normalizeSettings(await callWechatAction("settings"));
}

export async function saveWechatSettings(
  settings: WechatSettings,
): Promise<WechatSettings> {
  // The backend allowlist uses `wechatDbDir`; only send it when non-empty so
  // saving unrelated runtime limits never tries to switch the WeChat account.
  const payload: Record<string, unknown> = {
    pollIntervalSeconds: settings.pollIntervalSeconds,
    contextMessageLimit: settings.contextMessageLimit,
    contextCharacterLimit: settings.contextCharacterLimit,
  };
  if (settings.dbDir) payload.wechatDbDir = settings.dbDir;
  if (Array.isArray(settings.replyStyles)) payload.replyStyles = settings.replyStyles;
  if (settings.defaultTone !== undefined) payload.defaultTone = settings.defaultTone;
  if (settings.defaultTriggerMode !== undefined) payload.defaultTriggerMode = settings.defaultTriggerMode;
  for (const key of ["voiceTranscription", "voiceCommand", "voiceModelPath", "voiceArguments", "imageSearchRoots"]) {
    if (settings[key] !== undefined) payload[key] = settings[key];
  }
  return normalizeSettings(
    await callWechatAction("settings_save", { settings: payload }),
  );
}

export async function importProxytalk(
  path?: string,
): Promise<import("./types").WechatImportResult> {
  return callWechatAction("import_proxytalk", path ? { path } : {});
}

export async function selectWechatAccount(dbDir: string): Promise<void> {
  await callWechatAction("select", { dbDir });
}

export async function pollWechat(): Promise<WechatPollProject[]> {
  return normalizePoll(await callWechatAction("poll"));
}

export type {
  ReplyStyle,
  WechatChat,
  WechatConfigSnapshot,
  WechatDetectCandidate,
  WechatDetectResult,
  WechatDraft,
  WechatExportResult,
  WechatImageKeyResult,
  WechatImportResult,
  WechatMcpServer,
  WechatMediaPreview,
  WechatMessage,
  WechatPollProject,
  WechatProject,
  WechatProvider,
  WechatScanResult,
  WechatSettings,
};

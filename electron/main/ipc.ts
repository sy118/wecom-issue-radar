import { BrowserWindow, dialog, ipcMain, type IpcMainInvokeEvent } from "electron";

import {
  IPC_INVOKE_CHANNEL,
  type DialogOpenOptions,
  type DialogSaveOptions,
  type IpcCommand,
  type MainServices,
  type SmartSheetPayload,
} from "../shared/contracts";

interface IpcResult {
  ok: boolean;
  data?: unknown;
  error?: { message: string; code?: string };
}

const ALLOWED_DEV_ORIGINS = new Set(["http://localhost:1420", "http://127.0.0.1:1420"]);

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function isTrustedUrl(rawUrl: string | undefined): boolean {
  if (!rawUrl) return false;
  try {
    const url = new URL(rawUrl);
    if (url.protocol === "file:") return true;
    return ALLOWED_DEV_ORIGINS.has(url.origin);
  } catch {
    return false;
  }
}

function assertTrustedSender(event: IpcMainInvokeEvent): void {
  const frameUrl = event.senderFrame?.url ?? event.sender.getURL();
  if (!isTrustedUrl(frameUrl)) {
    throw new Error("请求来源不可信，已阻止本次操作。");
  }
}

function serializeError(error: unknown): { message: string; code?: string } {
  if (error instanceof Error) {
    const code = (error as Error & { code?: unknown }).code;
    return {
      message: error.message || "操作失败",
      ...(typeof code === "string" ? { code } : {}),
    };
  }
  if (typeof error === "string") return { message: error };
  return { message: "操作失败，请稍后重试。" };
}

function openDialogOptions(options: DialogOpenOptions) {
  return {
    title: options.title,
    defaultPath: options.defaultPath,
    properties: [
      ...(options.directory ? (["openDirectory"] as const) : (["openFile"] as const)),
      ...(options.multiple ? (["multiSelections"] as const) : []),
    ],
    filters: options.filters,
  };
}

function saveDialogOptions(options: DialogSaveOptions) {
  return {
    title: options.title,
    defaultPath: options.defaultPath,
    filters: options.filters,
  };
}

async function dispatch(
  command: IpcCommand,
  payload: unknown,
  services: MainServices,
  event: IpcMainInvokeEvent,
): Promise<unknown> {
  const record = asRecord(payload);

  switch (command) {
    case "bootstrap":
      return services.bootstrap();
    case "save_config":
      return services.saveConfig(asRecord(record.config));
    case "export_config_backup":
      return services.exportConfigBackup(asString(record.path));
    case "import_config_backup":
      return services.importConfigBackup(asString(record.path));
    case "detect_environment":
      return services.detectEnvironment();
    case "list_groups":
      return services.listGroups();
    case "run_task":
      return services.runTask(asRecord(record.request));
    case "list_schedules":
      return services.listSchedules();
    case "save_schedules":
      return services.saveSchedules(Array.isArray(record.schedules) ? record.schedules : []);
    case "run_schedule_now":
      return services.runScheduleNow(asString(record.scheduleId));
    case "list_schedule_execution_history":
      return services.listScheduleExecutionHistory({
        page: asNumber(record.page, 1),
        pageSize: asNumber(record.pageSize, 20),
        scheduleId: typeof record.scheduleId === "string" ? record.scheduleId : null,
      });
    case "list_pending_smart_sheet_syncs":
      return services.listPendingSmartSheetSyncs();
    case "clear_pending_smart_sheet_syncs":
      return services.clearPendingSmartSheetSyncs(asStringArray(record.pendingIds));
    case "preview_smart_sheet":
      return services.previewSmartSheet(asRecord(record.payload) as SmartSheetPayload);
    case "sync_smart_sheet":
      return services.syncSmartSheet(asRecord(record.payload) as SmartSheetPayload);
    case "rescan_task_images":
      return services.rescanTaskImages(
        asString(record.dayDir),
        Array.isArray(record.messageIds)
          ? record.messageIds.filter((value): value is number => typeof value === "number")
          : [],
      );
    case "wechat_action":
      return services.wechatAction(asString(record.action), asRecord(record.payload));
    case "wechat_runtime_status":
      return services.wechatRuntimeStatus();
    case "wechat_fill_input":
      return services.wechatFillInput(asString(record.chatTitle), asString(record.text));
    case "daily_action":
      return services.dailyAction(asString(record.action), asRecord(record.payload));
    case "launch_key_extraction":
      return services.launchKeyExtraction();
    case "reply_runtime_execute":
      return services.replyRuntimeExecute(asRecord(record.command));
    case "reply_runtime_query":
      return services.replyRuntimeQuery(asRecord(record.query));
    case "prepare_update_install":
      return services.prepareUpdateInstall();
    case "cancel_update_install":
      return services.cancelUpdateInstall();
    case "open_path":
      return services.openPath(asString(record.path));
    case "open_agent_log_directory":
      return services.openAgentLogDirectory();
    case "open_documentation":
      return services.openDocumentation();
    case "window_minimize": {
      BrowserWindow.fromWebContents(event.sender)?.minimize();
      return null;
    }
    case "window_toggle_maximize": {
      const window = BrowserWindow.fromWebContents(event.sender);
      if (!window) return false;
      if (window.isMaximized()) window.unmaximize();
      else window.maximize();
      return window.isMaximized();
    }
    case "window_close": {
      BrowserWindow.fromWebContents(event.sender)?.close();
      return null;
    }
    case "window_is_maximized": {
      return BrowserWindow.fromWebContents(event.sender)?.isMaximized() ?? false;
    }
    case "dialog_open": {
      const options = asRecord(record) as DialogOpenOptions;
      const window = BrowserWindow.fromWebContents(event.sender);
      const result = window
        ? await dialog.showOpenDialog(window, openDialogOptions(options))
        : await dialog.showOpenDialog(openDialogOptions(options));
      if (result.canceled || result.filePaths.length === 0) return null;
      return options.multiple ? result.filePaths : result.filePaths[0];
    }
    case "dialog_save": {
      const options = asRecord(record) as DialogSaveOptions;
      const window = BrowserWindow.fromWebContents(event.sender);
      const result = window
        ? await dialog.showSaveDialog(window, saveDialogOptions(options))
        : await dialog.showSaveDialog(saveDialogOptions(options));
      return result.canceled ? null : result.filePath ?? null;
    }
    case "zoom_get":
      return services.getZoom();
    case "zoom_set": {
      const factor = asNumber(record.factor, 1);
      await services.setZoom(factor);
      event.sender.setZoomFactor(factor);
      return null;
    }
    case "updater_check":
      return services.checkForUpdates();
    case "updater_download":
      return services.downloadUpdate();
    case "updater_install":
      return services.installUpdate();
    case "updater_relaunch":
      return services.relaunch();
    case "legacy_install_check":
      return services.legacyInstallCheck();
    case "legacy_uninstall":
      return services.legacyUninstall(
        typeof record.uninstallerPath === "string" ? record.uninstallerPath : undefined,
      );
    default:
      throw new Error(`未知的桌面命令：${String(command)}`);
  }
}

export function registerIpcHandlers(services: MainServices): void {
  ipcMain.handle(
    IPC_INVOKE_CHANNEL,
    async (event, command: unknown, payload: unknown): Promise<IpcResult> => {
      try {
        assertTrustedSender(event);
        if (typeof command !== "string" || !command) {
          throw new Error("桌面命令无效");
        }
        const data = await dispatch(command as IpcCommand, payload, services, event);
        return { ok: true, data };
      } catch (error) {
        return { ok: false, error: serializeError(error) };
      }
    },
  );
}

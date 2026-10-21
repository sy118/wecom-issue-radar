/**
 * Shared IPC contract between the Electron main process, the preload bridge and
 * the renderer. Keep this file free of Electron and DOM imports so it can be
 * type-checked by the renderer, the preload bundle and the main process alike.
 */

export const IPC_INVOKE_CHANNEL = "desktop:invoke";
export const IPC_EVENT_CHANNEL = "desktop:event";

export const IPC_COMMANDS = [
  "bootstrap",
  "save_config",
  "export_config_backup",
  "import_config_backup",
  "detect_environment",
  "list_groups",
  "run_task",
  "list_schedules",
  "save_schedules",
  "run_schedule_now",
  "list_schedule_execution_history",
  "list_pending_smart_sheet_syncs",
  "clear_pending_smart_sheet_syncs",
  "preview_smart_sheet",
  "sync_smart_sheet",
  "rescan_task_images",
  "wechat_action",
  "wechat_fill_input",
  "search_action",
  "daily_action",
  "launch_key_extraction",
  "reply_runtime_execute",
  "reply_runtime_query",
  "prepare_update_install",
  "cancel_update_install",
  "open_path",
  "open_agent_log_directory",
  "open_documentation",
  "window_minimize",
  "window_toggle_maximize",
  "window_close",
  "window_is_maximized",
  "dialog_open",
  "dialog_save",
  "zoom_get",
  "zoom_set",
  "updater_check",
  "updater_download",
  "updater_install",
  "updater_relaunch",
  "legacy_install_check",
  "legacy_uninstall",
] as const;

export type IpcCommand = (typeof IPC_COMMANDS)[number];

export const IPC_EVENTS = [
  "reply-runtime-event",
  "pipeline-progress",
  "schedule-progress",
  "schedule-completed",
  "updater-progress",
  "window-maximized-changed",
  "legacy-install-detected",
] as const;

export type IpcEvent = (typeof IPC_EVENTS)[number];

export interface BootstrapPayload {
  config: Record<string, unknown>;
  configPath: string;
  appVersion: string;
}

export interface UpdateCheckResult {
  currentVersion: string;
  version: string;
  date?: string;
  body?: string;
}

export interface UpdateProgressEvent {
  status: "started" | "progress" | "finished" | "error";
  downloadedBytes?: number;
  totalBytes?: number | null;
  percent?: number | null;
  message?: string;
}

export interface LegacyInstallInfo {
  installed: boolean;
  version?: string;
  uninstallerPath?: string;
  installDir?: string;
}

export interface DialogOpenOptions {
  title?: string;
  defaultPath?: string;
  directory?: boolean;
  multiple?: boolean;
  filters?: Array<{ name: string; extensions: string[] }>;
}

export interface DialogSaveOptions {
  title?: string;
  defaultPath?: string;
  filters?: Array<{ name: string; extensions: string[] }>;
}

export interface SmartSheetPayload {
  dayDir?: string;
  date?: string;
  templateId?: string;
  definitionPath?: string;
  uploadImages?: boolean;
  expectedTemplateRevision?: string;
  expectedDocumentRevision?: string;
}

export interface TaskRunRequest {
  [key: string]: unknown;
}

export interface HistoryQuery {
  page: number;
  pageSize: number;
  scheduleId?: string | null;
}

export interface LegacyInstallInfoRequest {
  uninstallerPath?: string;
}

/** Host context handed to the service layer by the main process. */
export interface MainContext {
  appVersion: string;
  isPackaged: boolean;
  resourcesPath: string;
  projectRoot: string;
  dataRoot: string;
  configPath: string;
  runtimeDbPath: string;
  scheduleStatePath: string;
  /** Pushes an event to the renderer; drops it after the window is gone. */
  publish(event: IpcEvent, payload: unknown): void;
  /** Registers a callback invoked before the app quits. */
  onShutdown(listener: () => void | Promise<void>): void;
}

/**
 * Services implemented by the Electron main process. Every IPC command maps to
 * exactly one of these methods; modules may reject with an `Error` whose
 * `message` is already user-facing Chinese copy.
 */
export interface MainServices {
  start(): Promise<void>;
  shutdown(): Promise<void>;
  bootstrap(): Promise<BootstrapPayload>;
  saveConfig(config: Record<string, unknown>): Promise<BootstrapPayload>;
  exportConfigBackup(path: string): Promise<void>;
  importConfigBackup(path: string): Promise<BootstrapPayload>;
  detectEnvironment(): Promise<unknown>;
  listGroups(): Promise<unknown>;
  runTask(request: TaskRunRequest): Promise<unknown>;
  listSchedules(): Promise<unknown>;
  saveSchedules(schedules: unknown[]): Promise<unknown>;
  runScheduleNow(scheduleId: string): Promise<void>;
  listScheduleExecutionHistory(query: HistoryQuery): Promise<unknown>;
  listPendingSmartSheetSyncs(): Promise<unknown>;
  clearPendingSmartSheetSyncs(pendingIds: string[]): Promise<void>;
  previewSmartSheet(payload: SmartSheetPayload): Promise<unknown>;
  syncSmartSheet(payload: SmartSheetPayload): Promise<unknown>;
  rescanTaskImages(dayDir: string, messageIds?: number[]): Promise<unknown>;
  wechatAction(action: string, payload: Record<string, unknown>): Promise<unknown>;
  wechatFillInput(chatTitle: string, text: string): Promise<{
    filled: boolean;
    chatTitle: string;
    characters: number;
  }>;
  searchAction(action: string, payload: Record<string, unknown>): Promise<unknown>;
  dailyAction(action: string, payload: Record<string, unknown>): Promise<unknown>;
  launchKeyExtraction(): Promise<void>;
  replyRuntimeExecute(command: Record<string, unknown>): Promise<unknown>;
  replyRuntimeQuery(query: Record<string, unknown>): Promise<unknown>;
  prepareUpdateInstall(): Promise<void>;
  cancelUpdateInstall(): Promise<void>;
  openPath(path: string): Promise<void>;
  openAgentLogDirectory(): Promise<string>;
  openDocumentation(): Promise<void>;
  checkForUpdates(): Promise<UpdateCheckResult | null>;
  downloadUpdate(): Promise<void>;
  installUpdate(): Promise<void>;
  relaunch(): Promise<void>;
  currentVersion(): string;
  legacyInstallCheck(): Promise<LegacyInstallInfo>;
  legacyUninstall(uninstallerPath?: string): Promise<void>;
  getZoom(): Promise<number>;
  setZoom(factor: number): Promise<void>;
}

export interface DesktopApi {
  readonly platform: string;
  readonly versions: {
    app: string;
    electron: string;
    chrome: string;
    node: string;
  };
  invoke<T = unknown>(command: IpcCommand, payload?: unknown): Promise<T>;
  on(event: IpcEvent, handler: (payload: unknown) => void): () => void;
}

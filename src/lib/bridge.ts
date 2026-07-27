import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  AppConfig,
  BootstrapResult,
  EnvironmentDetection,
  GroupInfo,
  PendingScheduleSync,
  ScheduleDefinition,
  ScheduleExecutionHistoryPage,
  ScheduleEvent,
  SmartSheetPreview,
  SyncResult,
  TaskRequest,
  TaskResult,
} from "../types";

export const bridge = {
  bootstrap: () => invoke<BootstrapResult>("bootstrap"),
  saveConfig: (config: AppConfig) =>
    invoke<BootstrapResult>("save_config", { config }),
  exportConfigBackup: (path: string) =>
    invoke<void>("export_config_backup", { path }),
  importConfigBackup: (path: string) =>
    invoke<BootstrapResult>("import_config_backup", { path }),
  detectEnvironment: () =>
    invoke<EnvironmentDetection>("detect_environment"),
  listGroups: () => invoke<{ groups: GroupInfo[] }>("list_groups"),
  runTask: (request: TaskRequest) =>
    invoke<TaskResult>("run_task", { request }),
  listSchedules: () => invoke<ScheduleDefinition[]>("list_schedules"),
  saveSchedules: (schedules: ScheduleDefinition[]) =>
    invoke<ScheduleDefinition[]>("save_schedules", { schedules }),
  runScheduleNow: (scheduleId: string) =>
    invoke<void>("run_schedule_now", { scheduleId }),
  listScheduleExecutionHistory: (page: number, pageSize: number, scheduleId?: string) =>
    invoke<ScheduleExecutionHistoryPage>("list_schedule_execution_history", {
      page,
      pageSize,
      scheduleId: scheduleId || null,
    }),
  listPendingSmartSheetSyncs: () =>
    invoke<PendingScheduleSync[]>("list_pending_smart_sheet_syncs"),
  clearPendingSmartSheetSyncs: (pendingIds: string[]) =>
    invoke<void>("clear_pending_smart_sheet_syncs", { pendingIds }),
  previewSmartSheet: (dayDir: string, date: string, templateId: string, definitionPath: string) =>
    invoke<SmartSheetPreview>("preview_smart_sheet", {
      payload: { dayDir, date, templateId, definitionPath },
    }),
  syncSmartSheet: (
    dayDir: string,
    date: string,
    templateId: string,
    uploadImages: boolean,
    expectedTemplateRevision: string,
    definitionPath: string,
    expectedDocumentRevision: string,
  ) =>
    invoke<SyncResult>("sync_smart_sheet", {
      payload: {
        dayDir,
        date,
        templateId,
        uploadImages,
        expectedTemplateRevision,
        definitionPath,
        expectedDocumentRevision,
      },
    }),
  launchKeyExtraction: () => invoke<void>("launch_key_extraction"),
  openPath: (path: string) => invoke<void>("open_path", { path }),
  openDocumentation: () => invoke<void>("open_documentation"),
  onProgress: (handler: (message: string) => void): Promise<UnlistenFn> =>
    listen<{ message: string }>("pipeline-progress", (event) =>
      handler(event.payload.message),
    ),
  onScheduleProgress: (handler: (event: ScheduleEvent) => void): Promise<UnlistenFn> =>
    listen<ScheduleEvent>("schedule-progress", (event) => handler(event.payload)),
  onScheduleCompleted: (handler: (event: ScheduleEvent) => void): Promise<UnlistenFn> =>
    listen<ScheduleEvent>("schedule-completed", (event) => handler(event.payload)),
};

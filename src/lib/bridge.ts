import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  AppConfig,
  BootstrapResult,
  EnvironmentDetection,
  GroupInfo,
  ScheduleDefinition,
  ScheduleEvent,
  SyncResult,
  TaskRequest,
  TaskResult,
} from "../types";

export const bridge = {
  bootstrap: () => invoke<BootstrapResult>("bootstrap"),
  saveConfig: (config: AppConfig) =>
    invoke<BootstrapResult>("save_config", { config }),
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
  syncSmartSheet: (dayDir: string, date: string, uploadImages = true) =>
    invoke<SyncResult>("sync_smart_sheet", {
      payload: { dayDir, date, uploadImages },
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

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  AppConfig,
  BootstrapResult,
  EnvironmentDetection,
  GroupInfo,
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
  syncSmartSheet: (dayDir: string, date: string, uploadImages = true) =>
    invoke<SyncResult>("sync_smart_sheet", {
      payload: { dayDir, date, uploadImages },
    }),
  launchKeyExtraction: () => invoke<void>("launch_key_extraction"),
  openPath: (path: string) => invoke<void>("open_path", { path }),
  onProgress: (handler: (message: string) => void): Promise<UnlistenFn> =>
    listen<{ message: string }>("pipeline-progress", (event) =>
      handler(event.payload.message),
    ),
};

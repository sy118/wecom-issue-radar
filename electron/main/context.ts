import os from "node:os";
import path from "node:path";
import { app, type BrowserWindow } from "electron";

import { IPC_EVENT_CHANNEL, type IpcEvent, type MainContext } from "../shared/contracts";
import {
  resolveAgentLogDirectory,
  resolveConfigPath,
  resolveDataRoot,
  resolveRuntimeDatabasePath,
  resolveScheduleStatePath,
  type PathEnvironment,
} from "../shared/paths";

export interface RuntimePaths {
  dataRoot: string;
  configPath: string;
  runtimeDbPath: string;
  scheduleStatePath: string;
  agentLogDirectory: string;
}

export function resolveRuntimePaths(env: NodeJS.ProcessEnv = process.env): RuntimePaths {
  const dataRoot = resolveDataRoot({ homeDir: os.homedir(), env });
  return {
    dataRoot,
    configPath: resolveConfigPath(dataRoot, env),
    runtimeDbPath: resolveRuntimeDatabasePath(dataRoot, env),
    scheduleStatePath: resolveScheduleStatePath(dataRoot, env),
    agentLogDirectory: resolveAgentLogDirectory(dataRoot),
  };
}

export function pathEnvironment(): PathEnvironment {
  return {
    homeDir: os.homedir(),
    env: process.env,
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    projectRoot: app.getAppPath(),
    platform: process.platform,
  };
}

export function createMainContext(
  getWindow: () => BrowserWindow | null,
): MainContext {
  const runtime = resolveRuntimePaths();
  const shutdownListeners: Array<() => void | Promise<void>> = [];

  return {
    appVersion: app.getVersion(),
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    projectRoot: app.getAppPath(),
    dataRoot: runtime.dataRoot,
    configPath: runtime.configPath,
    runtimeDbPath: runtime.runtimeDbPath,
    scheduleStatePath: runtime.scheduleStatePath,
    publish(event: IpcEvent, payload: unknown) {
      const window = getWindow();
      if (!window || window.isDestroyed()) return;
      window.webContents.send(IPC_EVENT_CHANNEL, event, payload);
    },
    onShutdown(listener) {
      shutdownListeners.push(listener);
    },
  };
}

export async function runShutdownHooks(
  listeners: Array<() => void | Promise<void>>,
): Promise<void> {
  for (const listener of listeners.splice(0)) {
    try {
      await listener();
    } catch (error) {
      console.error("shutdown hook failed", error instanceof Error ? error.message : error);
    }
  }
}

export function defaultLogDirectory(dataRoot: string): string {
  return resolveAgentLogDirectory(dataRoot);
}

export function ensureDirectory(directory: string): string {
  // Kept next to path resolution so callers do not re-implement it.
  void directory;
  return directory;
}

export const projectRootPath = (): string => path.resolve(app.getAppPath());

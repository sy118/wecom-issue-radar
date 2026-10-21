import { contextBridge, ipcRenderer } from "electron";

import {
  IPC_COMMANDS,
  IPC_EVENT_CHANNEL,
  IPC_EVENTS,
  IPC_INVOKE_CHANNEL,
  type DesktopApi,
  type IpcCommand,
  type IpcEvent,
} from "../shared/contracts";

const allowedCommands = new Set<string>(IPC_COMMANDS);
const allowedEvents = new Set<string>(IPC_EVENTS);

interface IpcEnvelope {
  ok: boolean;
  data?: unknown;
  error?: { message?: string; code?: string };
}

const api: DesktopApi = {
  platform: process.platform,
  versions: {
    app: "",
    electron: process.versions.electron ?? "",
    chrome: process.versions.chrome ?? "",
    node: process.versions.node ?? "",
  },
  invoke<T = unknown>(command: IpcCommand, payload?: unknown): Promise<T> {
    if (!allowedCommands.has(command)) {
      return Promise.reject(new Error(`未允许的桌面命令：${String(command)}`));
    }
    return ipcRenderer
      .invoke(IPC_INVOKE_CHANNEL, command, payload)
      .then((result: IpcEnvelope) => {
        if (!result || typeof result !== "object") {
          throw new Error("桌面桥接返回了无效结果。");
        }
        if (!result.ok) {
          const error = new Error(result.error?.message || "操作失败，请稍后重试。");
          if (result.error?.code) {
            (error as Error & { code?: string }).code = result.error.code;
          }
          throw error;
        }
        return result.data as T;
      });
  },
  on(event: IpcEvent, handler: (payload: unknown) => void): () => void {
    if (!allowedEvents.has(event)) return () => {};
    const listener = (_event: unknown, name: string, payload: unknown) => {
      if (name === event) handler(payload);
    };
    ipcRenderer.on(IPC_EVENT_CHANNEL, listener);
    return () => {
      ipcRenderer.removeListener(IPC_EVENT_CHANNEL, listener);
    };
  },
};

contextBridge.exposeInMainWorld("desktop", api);

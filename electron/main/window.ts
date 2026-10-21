import path from "node:path";
import { app, BrowserWindow } from "electron";

import { IPC_EVENT_CHANNEL } from "../shared/contracts";

const DEV_SERVER_URL = "http://localhost:1420";

function rendererUrl(): string | null {
  if (app.isPackaged) return null;
  return process.env.ELECTRON_RENDERER_URL?.trim() || DEV_SERVER_URL;
}

export function createMainWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 1024,
    minHeight: 700,
    center: true,
    frame: false,
    show: false,
    backgroundColor: "#f5f7fa",
    title: "企微问题雷达",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
    },
  });

  window.once("ready-to-show", () => {
    window.show();
  });

  const publishMaximized = () => {
    if (window.isDestroyed()) return;
    window.webContents.send(IPC_EVENT_CHANNEL, "window-maximized-changed", {
      maximized: window.isMaximized(),
    });
  };
  window.on("maximize", publishMaximized);
  window.on("unmaximize", publishMaximized);

  const url = rendererUrl();
  if (url) {
    void window.loadURL(url);
  } else {
    void window.loadFile(path.join(app.getAppPath(), "frontend-dist", "index.html"));
  }

  return window;
}

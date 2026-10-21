import { app, BrowserWindow } from "electron";

import type { MainServices } from "../shared/contracts";
import { createMainContext } from "./context";
import { registerIpcHandlers } from "./ipc";
import { installSecurity } from "./security";
import { createMainServices } from "./services";
import { createMainWindow } from "./window";

let mainWindow: BrowserWindow | null = null;
let services: MainServices | null = null;
let shuttingDown = false;
let shutdownComplete = false;

const hasSingleInstanceLock = app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });

  app.whenReady().then(async () => {
    app.setAppUserModelId("com.sy118.wecom-issue-radar");
    installSecurity();

    const context = createMainContext(() => mainWindow);
    const startedServices = createMainServices(context);
    services = startedServices;
    registerIpcHandlers(startedServices);

    mainWindow = createMainWindow();
    mainWindow.on("closed", () => {
      mainWindow = null;
    });

    try {
      const initialZoom = await startedServices.getZoom();
      if (initialZoom !== 1 && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.setZoomFactor(initialZoom);
      }
    } catch (error) {
      console.error(
        "failed to apply saved zoom",
        error instanceof Error ? error.message : error,
      );
    }

    try {
      await startedServices.start();
    } catch (error) {
      console.error(
        "failed to start background services",
        error instanceof Error ? error.message : error,
      );
    }

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length > 0) return;
      mainWindow = createMainWindow();
    });
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });

  app.on("before-quit", (event) => {
    if (shutdownComplete) return;
    event.preventDefault();
    if (shuttingDown) return;
    shuttingDown = true;
    void (async () => {
      try {
        await services?.shutdown();
      } catch (error) {
        console.error(
          "failed to stop background services",
          error instanceof Error ? error.message : error,
        );
      } finally {
        shutdownComplete = true;
        app.quit();
      }
    })();
  });
}

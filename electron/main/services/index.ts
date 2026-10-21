/**
 * Main-process service assembly. One instance per app: `createMainServices`
 * wires the worker/scheduler/reply-runtime/updater/migration helpers behind the
 * `MainServices` contract consumed by `ipc.ts`.
 */

import type {
  BootstrapPayload,
  HistoryQuery,
  LegacyInstallInfo,
  MainContext,
  MainServices,
  SmartSheetPayload,
  TaskRunRequest,
  UpdateCheckResult,
} from "../../shared/contracts";
import { ConfigService } from "./config";
import { MigrateService } from "./migrate";
import { openAgentLogDirectory, openDocumentation, openPath } from "./open-path";
import { ReplyRuntimeSupervisor } from "./replyRuntime";
import { SchedulerService } from "./scheduler";
import { UpdaterService } from "./updater";
import { WorkerClient } from "./worker";
import { ZoomService } from "./zoom";

export function createMainServices(ctx: MainContext): MainServices {
  const worker = new WorkerClient({
    projectRoot: ctx.projectRoot,
    resourcesPath: ctx.resourcesPath,
    isPackaged: ctx.isPackaged,
    configPath: ctx.configPath,
    publish: ctx.publish,
  });
  const config = new ConfigService(worker, {
    configPath: ctx.configPath,
    appVersion: ctx.appVersion,
  });
  const scheduler = new SchedulerService(config, worker, {
    configPath: ctx.configPath,
    scheduleStatePath: ctx.scheduleStatePath,
    publish: ctx.publish,
  });
  const replyRuntime = new ReplyRuntimeSupervisor({
    projectRoot: ctx.projectRoot,
    resourcesPath: ctx.resourcesPath,
    isPackaged: ctx.isPackaged,
    configPath: ctx.configPath,
    publish: ctx.publish,
  });
  const updater = new UpdaterService(
    {
      appVersion: ctx.appVersion,
      isPackaged: ctx.isPackaged,
      publish: ctx.publish,
    },
    worker,
    replyRuntime,
  );
  const migrate = new MigrateService();
  const zoom = new ZoomService(ctx.dataRoot);

  let started = false;
  let shutdownPromise: Promise<void> | null = null;

  const services: MainServices = {
    async start(): Promise<void> {
      if (started) return;
      started = true;
      replyRuntime.start();
      scheduler.start();
    },

    shutdown(): Promise<void> {
      if (!shutdownPromise) {
        shutdownPromise = (async () => {
          await scheduler.stop();
          await replyRuntime.shutdown();
          await worker.shutdown();
        })().catch((error) => {
          // Shutdown must never reject: index.ts quits unconditionally.
          console.error("关闭主进程服务失败", error);
        });
      }
      return shutdownPromise;
    },

    bootstrap(): Promise<BootstrapPayload> {
      return config.bootstrap();
    },

    saveConfig(nextConfig: Record<string, unknown>): Promise<BootstrapPayload> {
      return scheduler.saveConfig(nextConfig);
    },

    exportConfigBackup(targetPath: string): Promise<void> {
      return config.exportConfigBackup(targetPath);
    },

    importConfigBackup(sourcePath: string): Promise<BootstrapPayload> {
      return config.importConfigBackup(sourcePath);
    },

    detectEnvironment(): Promise<unknown> {
      return worker.detectEnvironment();
    },

    listGroups(): Promise<unknown> {
      return worker.listGroups();
    },

    runTask(request: TaskRunRequest): Promise<unknown> {
      return scheduler.runTask(request);
    },

    listSchedules(): Promise<unknown> {
      return scheduler.listSchedules();
    },

    saveSchedules(schedules: unknown[]): Promise<unknown> {
      return scheduler.saveSchedules(schedules);
    },

    runScheduleNow(scheduleId: string): Promise<void> {
      return scheduler.runScheduleNow(scheduleId);
    },

    listScheduleExecutionHistory(query: HistoryQuery): Promise<unknown> {
      return scheduler.listScheduleExecutionHistory(query);
    },

    listPendingSmartSheetSyncs(): Promise<unknown> {
      return scheduler.listPendingSmartSheetSyncs();
    },

    clearPendingSmartSheetSyncs(pendingIds: string[]): Promise<void> {
      return scheduler.clearPendingSmartSheetSyncs(pendingIds);
    },

    previewSmartSheet(payload: SmartSheetPayload): Promise<unknown> {
      return worker.previewSmartSheet(payload);
    },

    syncSmartSheet(payload: SmartSheetPayload): Promise<unknown> {
      return worker.syncSmartSheet(payload);
    },

    launchKeyExtraction(): Promise<void> {
      return worker.launchKeyExtraction();
    },

    replyRuntimeExecute(command: Record<string, unknown>): Promise<unknown> {
      return replyRuntime.execute(command);
    },

    replyRuntimeQuery(query: Record<string, unknown>): Promise<unknown> {
      return replyRuntime.query(query);
    },

    prepareUpdateInstall(): Promise<void> {
      return updater.prepareUpdateInstall();
    },

    cancelUpdateInstall(): Promise<void> {
      return updater.cancelUpdateInstall();
    },

    openPath(targetPath: string): Promise<void> {
      return openPath(targetPath);
    },

    openAgentLogDirectory(): Promise<string> {
      return openAgentLogDirectory(ctx.dataRoot);
    },

    openDocumentation(): Promise<void> {
      return openDocumentation();
    },

    checkForUpdates(): Promise<UpdateCheckResult | null> {
      return updater.checkForUpdates();
    },

    downloadUpdate(): Promise<void> {
      return updater.downloadUpdate();
    },

    installUpdate(): Promise<void> {
      return updater.installUpdate();
    },

    relaunch(): Promise<void> {
      return updater.relaunch();
    },

    currentVersion(): string {
      return ctx.appVersion;
    },

    legacyInstallCheck(): Promise<LegacyInstallInfo> {
      return migrate.checkLegacyInstall();
    },

    legacyUninstall(uninstallerPath?: string): Promise<void> {
      return migrate.legacyUninstall(uninstallerPath);
    },

    getZoom(): Promise<number> {
      return zoom.getZoom();
    },

    setZoom(factor: number): Promise<void> {
      return zoom.setZoom(factor);
    },
  };

  // Defensive: some quit paths (crash handlers, tests) may invoke the host
  // shutdown hooks instead of the IPC layer. The service shutdown is idempotent,
  // so registering here cannot run twice.
  ctx.onShutdown(() => services.shutdown());

  return services;
}

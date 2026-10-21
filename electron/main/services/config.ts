/**
 * Configuration facade. All normalization, v3→v4 migration, backup
 * sanitization and secret restoration live in the Python worker (`config.*`
 * one-shot commands); TypeScript only resolves paths, keeps a memory cache for
 * the scheduler, and performs the local backup-file guard.
 */

import { stat } from "node:fs/promises";
import path from "node:path";

import type { BootstrapPayload } from "../../shared/contracts";
import { errorMessage, isRecord } from "./utils";
import type { WorkerClient } from "./worker";

export const MAX_CONFIG_BACKUP_BYTES = 16 * 1024 * 1024;

export interface ConfigServiceOptions {
  configPath: string;
  appVersion: string;
}

export interface BackupFileInfo {
  isFile: boolean;
  size: number;
}

function samePath(left: string, right: string): boolean {
  const leftResolved = path.resolve(left);
  const rightResolved = path.resolve(right);
  if (process.platform === "win32") {
    return leftResolved.toLowerCase() === rightResolved.toLowerCase();
  }
  return leftResolved === rightResolved;
}

/** Pure 16 MB / regular-file guard for imported backups (exported for tests). */
export function validateBackupFile(info: BackupFileInfo): void {
  if (!info.isFile) {
    throw new Error("选择的配置备份不是文件");
  }
  if (info.size > MAX_CONFIG_BACKUP_BYTES) {
    throw new Error("配置备份超过 16 MB，已拒绝导入");
  }
}

/** Normalizes the worker reply into the IPC bootstrap shape. */
export function normalizeBootstrapResult(
  raw: unknown,
  options: Pick<ConfigServiceOptions, "configPath" | "appVersion">,
): BootstrapPayload {
  if (!isRecord(raw) || !isRecord(raw.config)) {
    throw new Error("处理引擎返回的配置数据无效");
  }
  const configPath =
    typeof raw.configPath === "string" && raw.configPath.trim()
      ? raw.configPath.trim()
      : options.configPath;
  return {
    config: raw.config,
    configPath,
    appVersion: options.appVersion,
  };
}

export class ConfigService {
  private cached: Record<string, unknown> | null = null;

  constructor(
    private readonly worker: Pick<WorkerClient, "runWorkerAction">,
    private readonly options: ConfigServiceOptions,
  ) {}

  /** The last config loaded/saved through this service (scheduler read path). */
  getConfig(): Record<string, unknown> | null {
    return this.cached;
  }

  /** Loads the config through the worker once, then serves the cache. */
  async load(): Promise<Record<string, unknown>> {
    if (this.cached) return this.cached;
    const payload = await this.bootstrap();
    return payload.config;
  }

  async bootstrap(): Promise<BootstrapPayload> {
    const raw = await this.worker.runWorkerAction("config_bootstrap", {
      configPath: this.options.configPath,
    });
    const payload = normalizeBootstrapResult(raw, this.options);
    this.cached = payload.config;
    return payload;
  }

  /**
   * Saves a settings-page config snapshot. The Python worker preserves the
   * schedules and runtime partitions from disk (the front end may hold a stale
   * snapshot), matching the old `save_config_preserving_schedules` behavior.
   */
  async saveConfig(config: Record<string, unknown>): Promise<BootstrapPayload> {
    if (!isRecord(config)) {
      throw new Error("配置数据无效：必须是 JSON 对象");
    }
    const raw = await this.worker.runWorkerAction("config_save", {
      config,
      configPath: this.options.configPath,
      preserveSchedules: true,
    });
    const payload = normalizeBootstrapResult(raw, this.options);
    this.cached = payload.config;
    return payload;
  }

  /**
   * Saves a config snapshot with an explicit schedules array. This is the
   * scheduler path and must replace (or clear) schedules rather than inherit
   * them from disk.
   */
  async saveSchedules(
    config: Record<string, unknown>,
    schedules: unknown[],
  ): Promise<BootstrapPayload> {
    if (!isRecord(config)) {
      throw new Error("配置数据无效：必须是 JSON 对象");
    }
    if (!Array.isArray(schedules)) {
      throw new Error("定时任务数据无效：必须是数组");
    }
    const raw = await this.worker.runWorkerAction("config_save", {
      config: { ...config, schedules },
      configPath: this.options.configPath,
      preserveSchedules: false,
    });
    const payload = normalizeBootstrapResult(raw, this.options);
    this.cached = payload.config;
    return payload;
  }

  async exportConfigBackup(targetPath: string): Promise<void> {
    const trimmed = targetPath?.trim();
    if (!trimmed) {
      throw new Error("请选择配置备份的保存位置");
    }
    if (samePath(trimmed, this.options.configPath)) {
      throw new Error("备份文件不能覆盖当前正在使用的配置文件");
    }
    await this.worker.runWorkerAction("config_export_backup", {
      path: trimmed,
      configPath: this.options.configPath,
    });
  }

  async importConfigBackup(sourcePath: string): Promise<BootstrapPayload> {
    const trimmed = sourcePath?.trim();
    if (!trimmed) {
      throw new Error("请选择要导入的配置备份文件");
    }
    let info: BackupFileInfo;
    try {
      const metadata = await stat(trimmed);
      info = { isFile: metadata.isFile(), size: metadata.size };
    } catch (error) {
      throw new Error(`找不到配置备份文件：${trimmed}（${errorMessage(error)}）`);
    }
    validateBackupFile(info);
    const raw = await this.worker.runWorkerAction("config_import_backup", {
      path: trimmed,
      configPath: this.options.configPath,
    });
    const payload = normalizeBootstrapResult(raw, this.options);
    this.cached = payload.config;
    return payload;
  }
}

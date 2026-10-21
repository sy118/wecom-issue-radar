import { mkdtemp, open, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  ConfigService,
  MAX_CONFIG_BACKUP_BYTES,
  normalizeBootstrapResult,
  validateBackupFile,
} from "../config";

type WorkerCall = { action: string; payload: Record<string, unknown> };

function createFakeWorker(
  responder: (action: string, payload: Record<string, unknown>) => unknown = () => ({
    config: {},
    configPath: "C:/data/config.local.json",
  }),
): {
  worker: ConstructorParameters<typeof ConfigService>[0];
  calls: WorkerCall[];
} {
  const calls: WorkerCall[] = [];
  const worker = {
    async runWorkerAction(
      action: string,
      payload: Record<string, unknown> = {},
    ): Promise<unknown> {
      calls.push({ action, payload });
      return responder(action, payload);
    },
  };
  return {
    worker: worker as unknown as ConstructorParameters<typeof ConfigService>[0],
    calls,
  };
}

const options = {
  configPath: "C:\\data\\config.local.json",
  appVersion: "4.0.0",
};

describe("config backup guard", () => {
  it("accepts a regular file within the 16 MB limit", () => {
    expect(() =>
      validateBackupFile({ isFile: true, size: MAX_CONFIG_BACKUP_BYTES }),
    ).not.toThrow();
    expect(MAX_CONFIG_BACKUP_BYTES).toBe(16 * 1024 * 1024);
  });

  it("rejects non-files and backups larger than 16 MB", () => {
    expect(() => validateBackupFile({ isFile: false, size: 10 })).toThrow(
      "不是文件",
    );
    expect(() =>
      validateBackupFile({ isFile: true, size: MAX_CONFIG_BACKUP_BYTES + 1 }),
    ).toThrow("超过 16 MB");
  });
});

describe("bootstrap payload normalization", () => {
  it("always uses the host app version and falls back to the host config path", () => {
    expect(
      normalizeBootstrapResult({ config: { a: 1 } }, options),
    ).toEqual({
      config: { a: 1 },
      configPath: options.configPath,
      appVersion: "4.0.0",
    });
    expect(
      normalizeBootstrapResult(
        { config: {}, configPath: " C:/other.json " },
        options,
      ).configPath,
    ).toBe("C:/other.json");
  });

  it("rejects malformed worker payloads", () => {
    expect(() => normalizeBootstrapResult(null, options)).toThrow(
      "配置数据无效",
    );
    expect(() => normalizeBootstrapResult({ config: [] }, options)).toThrow(
      "配置数据无效",
    );
  });
});

describe("ConfigService worker contract", () => {
  it("bootstraps through config_bootstrap, passes the config path and caches", async () => {
    const { worker, calls } = createFakeWorker(() => ({
      config: { schedules: [{ id: "s1" }] },
      configPath: "C:/resolved.json",
    }));
    const service = new ConfigService(worker, options);
    const payload = await service.bootstrap();
    expect(payload).toEqual({
      config: { schedules: [{ id: "s1" }] },
      configPath: "C:/resolved.json",
      appVersion: "4.0.0",
    });
    expect(calls).toEqual([
      {
        action: "config_bootstrap",
        payload: { configPath: options.configPath },
      },
    ]);
    expect(service.getConfig()).toEqual({ schedules: [{ id: "s1" }] });
    // `load()` serves the cache instead of spawning a second worker.
    expect(await service.load()).toEqual({ schedules: [{ id: "s1" }] });
    expect(calls).toHaveLength(1);
  });

  it("saves settings with preserveSchedules=true and refreshes the cache", async () => {
    const { worker, calls } = createFakeWorker((_action, payload) => ({
      config: { ...(payload.config as Record<string, unknown>), saved: true },
      configPath: "C:/data/config.local.json",
    }));
    const service = new ConfigService(worker, options);
    const payload = await service.saveConfig({ schedules: [{ id: "s1" }] });
    expect(calls[0]).toMatchObject({
      action: "config_save",
      payload: {
        configPath: options.configPath,
        preserveSchedules: true,
      },
    });
    expect(payload.config.saved).toBe(true);
    expect(service.getConfig()).toEqual({ schedules: [{ id: "s1" }], saved: true });
  });

  it("saves schedules with preserveSchedules=false so the list can be replaced", async () => {
    const { worker, calls } = createFakeWorker((_action, payload) => ({
      config: payload.config as Record<string, unknown>,
      configPath: "C:/data/config.local.json",
    }));
    const service = new ConfigService(worker, options);
    const schedules = [{ id: "s1" }, { id: "s2" }];
    const payload = await service.saveSchedules({ mcp_servers: {} }, schedules);
    expect(calls[0]).toMatchObject({
      action: "config_save",
      payload: { preserveSchedules: false },
    });
    expect((calls[0]?.payload.config as Record<string, unknown>).schedules).toEqual(
      schedules,
    );
    expect(payload.config.schedules).toEqual(schedules);
  });

  it("refuses to export a backup over the live config file", async () => {
    const { worker, calls } = createFakeWorker();
    const service = new ConfigService(worker, options);
    await expect(
      service.exportConfigBackup(options.configPath),
    ).rejects.toThrow("不能覆盖当前正在使用的配置文件");
    expect(calls).toHaveLength(0);
    await service.exportConfigBackup("C:/data/backup.json");
    expect(calls[0]).toEqual({
      action: "config_export_backup",
      payload: { path: "C:/data/backup.json", configPath: options.configPath },
    });
  });
});

describe("ConfigService import pre-checks", () => {
  let directory: string;

  beforeEach(async () => {
    directory = await mkdtemp(path.join(os.tmpdir(), "config-import-"));
  });

  afterEach(async () => {
    await rm(directory, { recursive: true, force: true });
  });

  it("reports a missing backup file before spawning the worker", async () => {
    const { worker, calls } = createFakeWorker();
    const service = new ConfigService(worker, options);
    await expect(
      service.importConfigBackup(path.join(directory, "missing.json")),
    ).rejects.toThrow("找不到配置备份文件");
    expect(calls).toHaveLength(0);
  });

  it("rejects backups over 16 MB before spawning the worker", async () => {
    const file = path.join(directory, "big.json");
    const handle = await open(file, "w");
    await handle.truncate(MAX_CONFIG_BACKUP_BYTES + 1);
    await handle.close();
    const { worker, calls } = createFakeWorker();
    const service = new ConfigService(worker, options);
    await expect(service.importConfigBackup(file)).rejects.toThrow("超过 16 MB");
    expect(calls).toHaveLength(0);
  });

  it("imports a valid backup and refreshes the cache", async () => {
    const file = path.join(directory, "backup.json");
    await writeFile(
      file,
      JSON.stringify({ format: "wecom-issue-radar-config-backup", config: {} }),
      "utf8",
    );
    const { worker, calls } = createFakeWorker(() => ({
      config: { imported: true },
      configPath: "C:/data/config.local.json",
    }));
    const service = new ConfigService(worker, options);
    const payload = await service.importConfigBackup(file);
    expect(calls[0]).toEqual({
      action: "config_import_backup",
      payload: { path: file, configPath: options.configPath },
    });
    expect(payload.config).toEqual({ imported: true });
    expect(service.getConfig()).toEqual({ imported: true });
  });
});

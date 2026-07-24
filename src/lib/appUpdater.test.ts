import { describe, expect, it, vi } from "vitest";
import {
  createAppUpdater,
  type AppUpdateHandle,
  type AppUpdaterAdapter,
  type AppUpdaterDownloadEvent,
  type UpdaterState,
} from "./appUpdater";

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function createUpdate(
  download: AppUpdateHandle["download"] = async () => {},
  install: AppUpdateHandle["install"] = async () => {},
): AppUpdateHandle {
  return {
    currentVersion: "3.1.0",
    version: "3.2.0",
    date: "2026-07-24T08:00:00Z",
    body: "修复导出问题",
    download,
    install,
  };
}

function createAdapter(
  check: AppUpdaterAdapter["check"],
  overrides: Partial<Omit<AppUpdaterAdapter, "check">> = {},
): AppUpdaterAdapter {
  return {
    check,
    prepareInstall: async () => {},
    cancelInstallPreparation: async () => {},
    relaunch: async () => {},
    ...overrides,
  };
}

describe("app updater checks", () => {
  it("keeps a background no-update result silent and presents an interactive result", async () => {
    const check = vi.fn(async () => null);
    const updater = createAppUpdater(createAdapter(check));

    await updater.check();
    expect(updater.getState()).toMatchObject({
      status: "up-to-date",
      visible: false,
      checkMode: null,
      update: null,
      canInstall: false,
    });

    await updater.check({ interactive: true });
    expect(check).toHaveBeenCalledTimes(2);
    expect(updater.getState()).toMatchObject({
      status: "up-to-date",
      visible: true,
    });
  });

  it("deduplicates concurrent checks and lets an interactive caller promote a background check", async () => {
    const pending = deferred<AppUpdateHandle | null>();
    const check = vi.fn(() => pending.promise);
    const updater = createAppUpdater(createAdapter(check));

    const background = updater.check();
    expect(updater.getState()).toMatchObject({
      status: "checking",
      visible: false,
      checkMode: "background",
    });

    const interactive = updater.check({ interactive: true });
    expect(interactive).toBe(background);
    expect(updater.getState()).toMatchObject({
      visible: true,
      checkMode: "interactive",
    });

    pending.resolve(null);
    await background;
    expect(check).toHaveBeenCalledTimes(1);
    expect(updater.getState()).toMatchObject({
      status: "up-to-date",
      visible: true,
    });
  });

  it("caches update metadata and its install handle", async () => {
    const available = createUpdate();
    const check = vi.fn(async () => available);
    const updater = createAppUpdater(createAdapter(check));

    await updater.check();
    expect(updater.getState()).toMatchObject({
      status: "available",
      visible: true,
      update: {
        currentVersion: "3.1.0",
        version: "3.2.0",
        date: "2026-07-24T08:00:00Z",
        body: "修复导出问题",
      },
      canInstall: true,
    });

    updater.dismiss();
    await updater.check({ interactive: true });
    expect(check).toHaveBeenCalledTimes(1);
    expect(updater.getState()).toMatchObject({ status: "available", visible: true });
  });

  it.each([
    ["request failed: network timeout", "network"],
    ["request failed: latest.json returned 404 not found", "configuration"],
    ["no compatible artifact for target x86_64", "platform"],
  ] as const)("classifies check failure %s as %s", async (message, kind) => {
    const updater = createAppUpdater(createAdapter(async () => {
      throw new Error(message);
    }));

    await updater.check({ interactive: true });

    expect(updater.getState()).toMatchObject({
      status: "error",
      visible: true,
      error: { kind, stage: "check" },
    });
  });

  it("recovers after a synchronous adapter failure and handles cyclic error values", async () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.error = cyclic;
    const check = vi.fn<AppUpdaterAdapter["check"]>()
      .mockImplementationOnce(() => {
        throw cyclic;
      })
      .mockResolvedValueOnce(null);
    const updater = createAppUpdater(createAdapter(check));

    await updater.check({ interactive: true });
    expect(updater.getState()).toMatchObject({
      status: "error",
      error: { kind: "unknown", stage: "check" },
    });

    await updater.check({ interactive: true });
    expect(check).toHaveBeenCalledTimes(2);
    expect(updater.getState().status).toBe("up-to-date");
  });

  it("notifies subscribers with stable snapshots and supports unsubscribe", async () => {
    const updater = createAppUpdater(createAdapter(async () => null));
    const snapshots: UpdaterState[] = [];
    const unsubscribe = updater.subscribe(() => snapshots.push(updater.getState()));

    const initial = updater.getState();
    expect(updater.getState()).toBe(initial);
    await updater.check({ interactive: true });
    unsubscribe();
    updater.dismiss();

    expect(snapshots.map((snapshot) => snapshot.status)).toEqual([
      "checking",
      "up-to-date",
    ]);
    expect(snapshots[0]).not.toBe(snapshots[1]);
  });
});

describe("app updater installation", () => {
  it("downloads with a 30 minute timeout, prepares, installs, and relaunches in order", async () => {
    const calls: string[] = [];
    const download = vi.fn<AppUpdateHandle["download"]>(async (onEvent) => {
      calls.push("download");
      onEvent?.({ event: "Started", data: { contentLength: 1_000 } });
      onEvent?.({ event: "Progress", data: { chunkLength: 125 } });
      onEvent?.({ event: "Progress", data: { chunkLength: 375 } });
      onEvent?.({ event: "Finished" });
    });
    const install = vi.fn(async () => {
      calls.push("install");
    });
    const prepareInstall = vi.fn(async () => {
      calls.push("prepare");
    });
    const relaunch = vi.fn(async () => {
      calls.push("relaunch");
    });
    const updater = createAppUpdater(createAdapter(
      async () => createUpdate(download, install),
      { prepareInstall, relaunch },
    ));
    const snapshots: UpdaterState[] = [];
    updater.subscribe(() => snapshots.push(updater.getState()));

    await updater.check();
    await updater.install();

    expect(calls).toEqual(["download", "prepare", "install", "relaunch"]);
    expect(download).toHaveBeenCalledWith(expect.any(Function), {
      timeout: 30 * 60_000,
    });
    expect(snapshots).toEqual(expect.arrayContaining([
      expect.objectContaining({
        status: "downloading",
        progress: { downloadedBytes: 125, totalBytes: 1_000, percent: 12.5 },
      }),
      expect.objectContaining({
        status: "downloading",
        progress: { downloadedBytes: 500, totalBytes: 1_000, percent: 50 },
      }),
      expect.objectContaining({
        status: "installing",
        progress: { downloadedBytes: 1_000, totalBytes: 1_000, percent: 100 },
      }),
    ]));
    expect(updater.getState()).toMatchObject({
      status: "restarting",
      canDismiss: false,
      canInstall: false,
    });
  });

  it("reports downloaded bytes without inventing a percentage when size is unknown", async () => {
    const downloading = deferred();
    const download = vi.fn<AppUpdateHandle["download"]>(async (onEvent) => {
      onEvent?.({ event: "Started", data: {} });
      onEvent?.({ event: "Progress", data: { chunkLength: 64 } });
      await downloading.promise;
    });
    const updater = createAppUpdater(createAdapter(
      async () => createUpdate(download),
    ));
    await updater.check();

    const installing = updater.install();
    await Promise.resolve();
    expect(updater.getState()).toMatchObject({
      status: "downloading",
      progress: { downloadedBytes: 64, totalBytes: null, percent: null },
    });

    downloading.resolve();
    await installing;
  });

  it("keeps a completed download when active work blocks preparation", async () => {
    const download = vi.fn<AppUpdateHandle["download"]>(async (onEvent) => {
      onEvent?.({ event: "Started", data: { contentLength: 10 } });
      onEvent?.({ event: "Progress", data: { chunkLength: 10 } });
      onEvent?.({ event: "Finished" });
    });
    const install = vi.fn(async () => {});
    const prepareInstall = vi.fn<AppUpdaterAdapter["prepareInstall"]>()
      .mockRejectedValueOnce(new Error("update busy: active task is running"))
      .mockResolvedValueOnce();
    const cancelInstallPreparation = vi.fn(async () => {});
    const updater = createAppUpdater(createAdapter(
      async () => createUpdate(download, install),
      { prepareInstall, cancelInstallPreparation },
    ));
    await updater.check();

    await updater.install();
    expect(updater.getState()).toMatchObject({
      status: "error",
      error: { kind: "busy", stage: "prepare" },
      progress: { downloadedBytes: 10, totalBytes: 10, percent: 100 },
      canInstall: true,
    });
    expect(download).toHaveBeenCalledTimes(1);
    expect(install).not.toHaveBeenCalled();
    expect(cancelInstallPreparation).not.toHaveBeenCalled();

    const retry = updater.install();
    expect(updater.getState().status).toBe("installing");
    await retry;
    expect(download).toHaveBeenCalledTimes(1);
    expect(prepareInstall).toHaveBeenCalledTimes(2);
    expect(install).toHaveBeenCalledTimes(1);
    expect(updater.getState().status).toBe("restarting");
  });

  it("cancels preparation after install failure and retries without downloading again", async () => {
    const download = vi.fn<AppUpdateHandle["download"]>(async (onEvent) => {
      onEvent?.({ event: "Finished" });
    });
    const install = vi.fn<AppUpdateHandle["install"]>()
      .mockRejectedValueOnce(new Error("installer exited with code 1"))
      .mockResolvedValueOnce();
    const prepareInstall = vi.fn(async () => {});
    const cancelInstallPreparation = vi.fn(async () => {});
    const relaunch = vi.fn(async () => {});
    const updater = createAppUpdater(createAdapter(
      async () => createUpdate(download, install),
      { prepareInstall, cancelInstallPreparation, relaunch },
    ));
    await updater.check();

    await updater.install();
    expect(updater.getState()).toMatchObject({
      status: "error",
      error: { kind: "installation", stage: "install" },
      canInstall: true,
    });
    expect(cancelInstallPreparation).toHaveBeenCalledTimes(1);

    await updater.install();
    expect(download).toHaveBeenCalledTimes(1);
    expect(prepareInstall).toHaveBeenCalledTimes(2);
    expect(install).toHaveBeenCalledTimes(2);
    expect(cancelInstallPreparation).toHaveBeenCalledTimes(1);
    expect(relaunch).toHaveBeenCalledTimes(1);
    expect(updater.getState().status).toBe("restarting");
  });

  it("classifies signature download failures and does not prepare or install", async () => {
    const prepareInstall = vi.fn(async () => {});
    const install = vi.fn(async () => {});
    const relaunch = vi.fn(async () => {});
    const updater = createAppUpdater(createAdapter(
      async () => createUpdate(async () => {
        throw new Error("signature verification failed: invalid public key");
      }, install),
      { prepareInstall, relaunch },
    ));
    await updater.check();

    await updater.install();

    expect(updater.getState()).toMatchObject({
      status: "error",
      error: { kind: "signature", stage: "download" },
      canInstall: true,
    });
    expect(prepareInstall).not.toHaveBeenCalled();
    expect(install).not.toHaveBeenCalled();
    expect(relaunch).not.toHaveBeenCalled();
  });

  it("uses a separate restart-required state when the fallback relaunch fails", async () => {
    const updater = createAppUpdater(createAdapter(
      async () => createUpdate(async (onEvent) => {
        onEvent?.({ event: "Finished" });
      }),
      {
        relaunch: async () => {
          throw new Error("restart command denied");
        },
      },
    ));
    await updater.check();

    await updater.install();

    expect(updater.getState()).toMatchObject({
      status: "restart-required",
      visible: true,
      error: { kind: "restart", stage: "restart" },
      canDismiss: true,
      canInstall: false,
    });
  });

  it("deduplicates installs and cannot be dismissed while installation is active", async () => {
    const downloading = deferred();
    let onEvent: ((event: AppUpdaterDownloadEvent) => void) | undefined;
    const download = vi.fn<AppUpdateHandle["download"]>(async (listener) => {
      onEvent = listener;
      onEvent?.({ event: "Started", data: { contentLength: 10 } });
      await downloading.promise;
    });
    const updater = createAppUpdater(createAdapter(
      async () => createUpdate(download),
    ));
    await updater.check();

    const first = updater.install();
    const second = updater.install();
    expect(second).toBe(first);
    await Promise.resolve();
    updater.dismiss();
    expect(updater.getState()).toMatchObject({
      status: "downloading",
      visible: true,
      canDismiss: false,
    });

    onEvent?.({ event: "Finished" });
    updater.dismiss();
    expect(updater.getState()).toMatchObject({
      status: "installing",
      visible: true,
      canDismiss: false,
    });

    downloading.resolve();
    await first;
  });
});

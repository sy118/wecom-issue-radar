import { invoke } from "@tauri-apps/api/core";
import { relaunch } from "@tauri-apps/plugin-process";
import { check as checkForUpdate } from "@tauri-apps/plugin-updater";

export type UpdaterStatus =
  | "idle"
  | "checking"
  | "up-to-date"
  | "available"
  | "downloading"
  | "installing"
  | "restarting"
  | "restart-required"
  | "error";

export type UpdaterErrorKind =
  | "network"
  | "signature"
  | "platform"
  | "configuration"
  | "busy"
  | "installation"
  | "restart"
  | "unknown";

export type UpdaterErrorStage =
  | "check"
  | "download"
  | "prepare"
  | "install"
  | "restart";

export interface AvailableUpdate {
  readonly currentVersion: string;
  readonly version: string;
  readonly date?: string;
  readonly body?: string;
}

export interface UpdaterProgress {
  readonly downloadedBytes: number;
  readonly totalBytes: number | null;
  /** Null when the server does not report a content length. */
  readonly percent: number | null;
}

export interface UpdaterError {
  readonly kind: UpdaterErrorKind;
  readonly stage: UpdaterErrorStage;
  readonly message: string;
}

export interface UpdaterState {
  readonly status: UpdaterStatus;
  readonly visible: boolean;
  readonly checkMode: "background" | "interactive" | null;
  readonly update: AvailableUpdate | null;
  readonly progress: UpdaterProgress | null;
  readonly error: UpdaterError | null;
  readonly canDismiss: boolean;
  readonly canInstall: boolean;
}

export type AppUpdaterDownloadEvent =
  | { event: "Started"; data: { contentLength?: number } }
  | { event: "Progress"; data: { chunkLength: number } }
  | { event: "Finished" };

export interface AppUpdaterDownloadOptions {
  readonly timeout?: number;
}

/** The internal seam used by the production Tauri adapter and deterministic tests. */
export interface AppUpdateHandle extends AvailableUpdate {
  download(
    onEvent?: (event: AppUpdaterDownloadEvent) => void,
    options?: AppUpdaterDownloadOptions,
  ): Promise<void>;
  install(): Promise<void>;
}

export interface AppUpdaterAdapter {
  check(): Promise<AppUpdateHandle | null>;
  prepareInstall(): Promise<void>;
  cancelInstallPreparation(): Promise<void>;
  relaunch(): Promise<void>;
}

export interface AppUpdaterController {
  getState(): UpdaterState;
  subscribe(listener: () => void): () => void;
  /** Checks silently by default. Interactive checks present all outcomes to the user. */
  check(options?: { interactive?: boolean }): Promise<void>;
  /** Downloads, verifies, installs, and then relaunches the cached update. */
  install(): Promise<void>;
  dismiss(): void;
  show(): void;
}

type StateCore = Omit<UpdaterState, "canDismiss" | "canInstall">;

const ACTIVE_INSTALL_STATUSES = new Set<UpdaterStatus>([
  "downloading",
  "installing",
  "restarting",
]);

const CHECK_TIMEOUT_MS = 30_000;
const DOWNLOAD_TIMEOUT_MS = 30 * 60_000;

const productionAdapter: AppUpdaterAdapter = {
  check: async () => {
    const update = await checkForUpdate({ timeout: CHECK_TIMEOUT_MS });
    if (!update) return null;
    return {
      currentVersion: update.currentVersion,
      version: update.version,
      ...(update.date ? { date: update.date } : {}),
      ...(update.body ? { body: update.body } : {}),
      download: (onEvent, options) => update.download(onEvent, options),
      install: () => update.install(),
    };
  },
  prepareInstall: () => invoke("prepare_update_install"),
  cancelInstallPreparation: () => invoke("cancel_update_install"),
  relaunch,
};

function errorText(reason: unknown, seen = new Set<object>()): string {
  if (typeof reason === "string") return reason;
  if (reason instanceof Error) return reason.message;
  if (reason && typeof reason === "object") {
    if (seen.has(reason)) return "";
    seen.add(reason);
    const record = reason as Record<string, unknown>;
    for (const key of ["message", "error", "reason", "detail"]) {
      const nested = errorText(record[key], seen);
      if (nested) return nested;
    }
  }
  return "";
}

function updaterError(reason: unknown, stage: UpdaterErrorStage): UpdaterError {
  if (stage === "restart") {
    return {
      kind: "restart",
      stage,
      message: "更新已经安装，但自动重启失败，请手动重启应用。",
    };
  }

  const text = errorText(reason).toLowerCase();
  if (stage === "prepare" && /群监听.*(?:运行|启用)|(?:enabled|active|ready) listeners?/.test(text)) {
    return {
      kind: "busy",
      stage,
      message: "请先在“群监听回复”中停用正在运行的监听器，待检索完成后重试更新。",
    };
  }
  if (stage === "prepare" && /\bbusy\b|active (?:task|job|run|export)|(?:task|job).*(?:running|active)|任务.*(?:正在运行|正在执行|尚未结束|进行中)|(?:正在运行|正在执行|进行中).*任务|导出.*(?:运行中|进行中)/.test(text)) {
    return {
      kind: "busy",
      stage,
      message: "有任务正在运行，请等待任务完成后重试更新。",
    };
  }
  if (/signature|public\s*key|pubkey|cryptograph|verify|verification/.test(text)) {
    return {
      kind: "signature",
      stage,
      message: "更新包签名校验失败，已停止安装。",
    };
  }
  if (/platform|architecture|\barch\b|unsupported (?:system|target)|no (?:compatible|suitable)|(?:artifact|package).*(?:target|platform)|target.*(?:artifact|package|not found|unsupported)/.test(text)) {
    return {
      kind: "platform",
      stage,
      message: "没有找到适用于当前系统的更新包。",
    };
  }
  if (/endpoint|manifest|latest\.json|invalid (?:updater )?json|\b(?:401|403|404)\b|not found|configuration|config/.test(text)) {
    return {
      kind: "configuration",
      stage,
      message: "更新服务配置有误，请联系维护人员。",
    };
  }
  if (/network|timeout|timed out|connection|failed to fetch|request failed|dns|econn|enotfound|offline|tls/.test(text)) {
    return {
      kind: "network",
      stage,
      message: "无法连接更新服务器，请检查网络后重试。",
    };
  }
  if (stage === "prepare" || stage === "install") {
    return {
      kind: "installation",
      stage,
      message: "更新安装失败，请稍后重试。",
    };
  }
  return {
    kind: "unknown",
    stage,
    message: stage === "check"
      ? "检查更新失败，请稍后重试。"
      : "更新下载失败，请稍后重试。",
  };
}

function updateMetadata(update: AppUpdateHandle): AvailableUpdate {
  return {
    currentVersion: update.currentVersion,
    version: update.version,
    ...(update.date ? { date: update.date } : {}),
    ...(update.body ? { body: update.body } : {}),
  };
}

function normalizedByteCount(value: number | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : null;
}

function progress(downloadedBytes: number, totalBytes: number | null): UpdaterProgress {
  return {
    downloadedBytes,
    totalBytes,
    percent: totalBytes === null
      ? null
      : Math.min(100, (downloadedBytes / totalBytes) * 100),
  };
}

export function createAppUpdater(
  adapter: AppUpdaterAdapter = productionAdapter,
): AppUpdaterController {
  const listeners = new Set<() => void>();
  let cachedUpdate: AppUpdateHandle | null = null;
  let downloaded = false;
  let checkPromise: Promise<void> | null = null;
  let installPromise: Promise<void> | null = null;
  let suppressCheckPresentation = false;
  let state: UpdaterState = Object.freeze({
    status: "idle",
    visible: false,
    checkMode: null,
    update: null,
    progress: null,
    error: null,
    canDismiss: true,
    canInstall: false,
  });

  const transition = (patch: Partial<StateCore>) => {
    const core: StateCore = {
      status: patch.status ?? state.status,
      visible: patch.visible ?? state.visible,
      checkMode: patch.checkMode === undefined ? state.checkMode : patch.checkMode,
      update: patch.update === undefined ? state.update : patch.update,
      progress: patch.progress === undefined ? state.progress : patch.progress,
      error: patch.error === undefined ? state.error : patch.error,
    };
    state = Object.freeze({
      ...core,
      // Hiding the dialog never cancels an active download or installation.
      // Keep the application usable while the updater continues in the background.
      canDismiss: true,
      canInstall: cachedUpdate !== null
        && (core.status === "available" || core.status === "error"),
    });
    listeners.forEach((listener) => listener());
  };

  const getState = () => state;

  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  };

  const check = (options: { interactive?: boolean } = {}): Promise<void> => {
    const interactive = options.interactive === true;

    if (installPromise || ACTIVE_INSTALL_STATUSES.has(state.status)) {
      return installPromise ?? Promise.resolve();
    }
    if (cachedUpdate) {
      if (interactive) {
        transition({
          status: "available",
          visible: true,
          checkMode: null,
          progress: null,
          error: null,
        });
      }
      return Promise.resolve();
    }
    if (checkPromise) {
      if (interactive) {
        suppressCheckPresentation = false;
        if (!state.visible || state.checkMode !== "interactive") {
          transition({ visible: true, checkMode: "interactive" });
        }
      }
      return checkPromise;
    }

    suppressCheckPresentation = false;
    transition({
      status: "checking",
      visible: interactive || state.visible,
      checkMode: interactive ? "interactive" : "background",
      progress: null,
      error: null,
    });

    const operation = (async () => {
      // Defer the adapter call until checkPromise is assigned. An adapter that
      // throws synchronously must not leave the controller permanently busy.
      await Promise.resolve();
      try {
        const update = await adapter.check();
        const wasInteractive = state.checkMode === "interactive";
        if (update) {
          cachedUpdate = update;
          downloaded = false;
          transition({
            status: "available",
            visible: !suppressCheckPresentation,
            checkMode: null,
            update: updateMetadata(update),
            progress: null,
            error: null,
          });
        } else {
          transition({
            status: "up-to-date",
            visible: wasInteractive && !suppressCheckPresentation
              ? true
              : state.visible,
            checkMode: null,
            update: null,
            progress: null,
            error: null,
          });
        }
      } catch (reason) {
        const wasInteractive = state.checkMode === "interactive";
        transition({
          status: "error",
          visible: wasInteractive && !suppressCheckPresentation
            ? true
            : state.visible,
          checkMode: null,
          update: null,
          progress: null,
          error: updaterError(reason, "check"),
        });
      } finally {
        checkPromise = null;
      }
    })();
    checkPromise = operation;
    return operation;
  };

  const install = (): Promise<void> => {
    if (installPromise) return installPromise;
    if (!cachedUpdate || checkPromise) return Promise.resolve();

    const update = cachedUpdate;
    transition({
      status: downloaded ? "installing" : "downloading",
      visible: true,
      checkMode: null,
      progress: downloaded ? state.progress : progress(0, null),
      error: null,
    });

    const operation = (async () => {
      // See the matching check() guard above for why this begins with a yield.
      await Promise.resolve();
      let stage: UpdaterErrorStage = downloaded ? "prepare" : "download";
      let installPrepared = false;
      try {
        if (!downloaded) {
          let downloadFinished = false;
          await update.download((event) => {
            if (event.event === "Started") {
              transition({
                status: "downloading",
                progress: progress(0, normalizedByteCount(event.data.contentLength)),
              });
              return;
            }
            if (event.event === "Progress") {
              const previous = state.progress ?? progress(0, null);
              const chunkLength = normalizedByteCount(event.data.chunkLength) ?? 0;
              transition({
                status: "downloading",
                progress: progress(
                  previous.downloadedBytes + chunkLength,
                  previous.totalBytes,
                ),
              });
              return;
            }

            downloadFinished = true;
            const previous = state.progress ?? progress(0, null);
            transition({
              status: "installing",
              progress: previous.totalBytes === null
                ? previous
                : progress(previous.totalBytes, previous.totalBytes),
            });
          }, {
            timeout: DOWNLOAD_TIMEOUT_MS,
          });
          downloaded = true;
          if (!downloadFinished) transition({ status: "installing" });
        }

        stage = "prepare";
        await adapter.prepareInstall();
        installPrepared = true;
        stage = "install";
        await update.install();

        cachedUpdate = null;
        downloaded = false;
        // updater 2.10.1 terminates the old process during a successful
        // Windows install. This is the fallback for platforms where the
        // install promise resolves in the current process.
        transition({
          status: "restarting",
          error: null,
        });
        try {
          await adapter.relaunch();
        } catch (reason) {
          transition({
            status: "restart-required",
            visible: true,
            error: updaterError(reason, "restart"),
          });
        }
      } catch (reason) {
        if (installPrepared) {
          try {
            await adapter.cancelInstallPreparation();
          } catch {
            // Preserve the original install failure. The backend keeps the
            // safety gate closed if cancellation itself cannot be confirmed.
          }
        }
        transition({
          status: "error",
          visible: true,
          error: updaterError(reason, stage),
        });
      } finally {
        installPromise = null;
      }
    })();
    installPromise = operation;
    return operation;
  };

  const dismiss = () => {
    if (!state.visible || !state.canDismiss) return;
    if (state.status === "checking") suppressCheckPresentation = true;
    transition({ visible: false });
  };

  const show = () => {
    if (state.status === "checking") suppressCheckPresentation = false;
    if (!state.visible) transition({ visible: true });
  };

  return {
    getState,
    subscribe,
    check,
    install,
    dismiss,
    show,
  };
}

export const appUpdater = createAppUpdater();

/**
 * Auto-update service built on `electron-updater`.
 *
 * `autoDownload` and `autoInstallOnAppQuit` are disabled: the renderer drives
 * "check → download → prepare → install" explicitly. Before installing, all
 * background work must be quiesced (one-shot workers and the resident reply
 * runtime) so an update never corrupts the SQLite runtime or leaves orphans.
 */

import { app } from "electron";
import { autoUpdater, type ProgressInfo } from "electron-updater";

import type { MainContext, UpdateCheckResult } from "../../shared/contracts";
import { compareVersions, errorMessage, trimmedString } from "./utils";
import type { WorkerClient } from "./worker";

export interface UpdaterServiceContext {
  appVersion: string;
  isPackaged: boolean;
  publish: MainContext["publish"];
}

export interface UpdaterWorkerHook {
  prepareUpdateInstall(): void;
  cancelUpdateInstall(): void;
}

export interface ReplyRuntimeUpdateHook {
  prepareForUpdate(): Promise<void>;
  resumeAfterUpdate(): Promise<void>;
}

function formatReleaseNotes(notes: unknown): string | undefined {
  if (typeof notes === "string") {
    const trimmed = notes.trim();
    return trimmed || undefined;
  }
  if (Array.isArray(notes)) {
    const text = notes
      .map((note) => {
        if (typeof note === "string") return note;
        if (note && typeof note === "object") {
          const record = note as Record<string, unknown>;
          const version = trimmedString(record.version);
          const body = trimmedString(record.note);
          if (body) return version ? `${version}\n${body}` : body;
        }
        return "";
      })
      .filter(Boolean)
      .join("\n\n");
    return text.trim() || undefined;
  }
  return undefined;
}

/** Maps updater library failures to actionable Chinese copy. */
export function updaterErrorMessage(stage: string, error: unknown): string {
  const text = errorMessage(error);
  const normalized = text.toLowerCase();
  if (/signature|public key|pubkey|verify|verification|cryptograph/.test(normalized)) {
    return "更新包签名校验失败，已停止安装。";
  }
  if (
    /network|timeout|timed out|econn|enotfound|eai_again|socket|tls|certificate|offline|fetch failed/.test(
      normalized,
    )
  ) {
    return "无法连接更新服务器，请检查网络后重试。";
  }
  if (/404|not found|manifest|latest\.ya?ml|cannot find/.test(normalized)) {
    return "更新服务配置有误，请联系维护人员。";
  }
  if (/no published versions|no update info/.test(normalized)) {
    return "没有可用的更新包，请稍后重试。";
  }
  return `${stage}失败：${text}`;
}

export class UpdaterService {
  private prepared = false;
  private downloading = false;
  private readonly onDownloadProgress = (info: ProgressInfo): void => {
    const total = typeof info.total === "number" && info.total > 0 ? info.total : null;
    this.ctx.publish("updater-progress", {
      status: "progress",
      downloadedBytes: info.transferred,
      totalBytes: total,
      percent:
        typeof info.percent === "number" && Number.isFinite(info.percent)
          ? Math.max(0, Math.min(100, info.percent))
          : null,
    });
  };

  constructor(
    private readonly ctx: UpdaterServiceContext,
    private readonly worker: UpdaterWorkerHook,
    private readonly replyRuntime: ReplyRuntimeUpdateHook,
  ) {
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = false;
    autoUpdater.on("download-progress", this.onDownloadProgress);
  }

  async checkForUpdates(): Promise<UpdateCheckResult | null> {
    if (!this.ctx.isPackaged) {
      // Development builds have no update metadata; the UI treats null as
      // "already up to date" instead of surfacing an error.
      return null;
    }
    try {
      const result = await autoUpdater.checkForUpdates();
      const info = result?.updateInfo;
      if (!info || !trimmedString(info.version)) {
        return null;
      }
      if (compareVersions(info.version, this.ctx.appVersion) <= 0) {
        return null;
      }
      const body = formatReleaseNotes(info.releaseNotes);
      return {
        currentVersion: this.ctx.appVersion,
        version: info.version,
        ...(info.releaseDate ? { date: String(info.releaseDate) } : {}),
        ...(body ? { body } : {}),
      };
    } catch (error) {
      throw new Error(updaterErrorMessage("检查更新", error));
    }
  }

  async downloadUpdate(): Promise<void> {
    this.assertPackaged();
    if (this.downloading) {
      throw new Error("更新正在下载，请稍候");
    }
    this.downloading = true;
    this.ctx.publish("updater-progress", { status: "started" });
    try {
      await autoUpdater.downloadUpdate();
      this.ctx.publish("updater-progress", { status: "finished" });
    } catch (error) {
      const message = updaterErrorMessage("更新下载", error);
      this.ctx.publish("updater-progress", { status: "error", message });
      throw new Error(message);
    } finally {
      this.downloading = false;
    }
  }

  /**
   * Quiesces the resident runtime and blocks new one-shot work. Called by the
   * renderer after downloading and before installing; failures roll back so the
   * app stays usable.
   */
  async prepareUpdateInstall(): Promise<void> {
    if (this.prepared) return;
    await this.replyRuntime.prepareForUpdate();
    try {
      this.worker.prepareUpdateInstall();
    } catch (error) {
      await this.replyRuntime.resumeAfterUpdate().catch(() => undefined);
      throw new Error(errorMessage(error));
    }
    this.prepared = true;
  }

  async cancelUpdateInstall(): Promise<void> {
    this.worker.cancelUpdateInstall();
    await this.replyRuntime.resumeAfterUpdate().catch(() => undefined);
    this.prepared = false;
  }

  /** Installs the downloaded update (quitting the app). */
  async installUpdate(): Promise<void> {
    this.assertPackaged();
    try {
      await this.prepareUpdateInstall();
    } catch (error) {
      throw error instanceof Error ? error : new Error(errorMessage(error));
    }
    try {
      autoUpdater.quitAndInstall(false, true);
    } catch (error) {
      await this.cancelUpdateInstall().catch(() => undefined);
      throw new Error(updaterErrorMessage("更新安装", error));
    }
  }

  async relaunch(): Promise<void> {
    app.relaunch();
    app.exit(0);
  }

  private assertPackaged(): void {
    if (!this.ctx.isPackaged) {
      throw new Error("仅安装版支持自动更新。");
    }
  }
}

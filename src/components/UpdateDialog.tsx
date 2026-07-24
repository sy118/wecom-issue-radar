import {
  CheckCircle2,
  Download,
  LoaderCircle,
  RefreshCw,
  RotateCw,
  ShieldAlert,
  Sparkles,
} from "lucide-react";
import { useEffect, useRef } from "react";
import type { UpdaterState } from "../lib/appUpdater";

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB"];
  let amount = value / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1024; index += 1) {
    amount /= 1024;
    unit = units[index];
  }
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${unit}`;
}

function formatReleaseDate(value?: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(date);
}

export function UpdateDialog({
  state,
  currentVersion,
  onInstall,
  onRetry,
  onDismiss,
}: {
  state: UpdaterState;
  currentVersion: string;
  onInstall: () => void;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  const dialogRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!state.visible) return;
    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const dialog = dialogRef.current;
    if (!dialog) return;

    const focusableSelector = [
      "button:not(:disabled)",
      "[href]",
      "input:not(:disabled)",
      "select:not(:disabled)",
      "textarea:not(:disabled)",
      '[tabindex]:not([tabindex="-1"])',
    ].join(",");
    const focusable = () => Array.from(
      dialog.querySelectorAll<HTMLElement>(focusableSelector),
    );
    (focusable()[0] ?? dialog).focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && state.canDismiss) {
        event.preventDefault();
        onDismiss();
        return;
      }
      if (event.key !== "Tab") return;
      const elements = focusable();
      if (elements.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [onDismiss, state.canDismiss, state.visible]);

  if (!state.visible) return null;

  const releaseDate = formatReleaseDate(state.update?.date);
  const busy = ["checking", "downloading", "installing", "restarting"].includes(
    state.status,
  );
  const progressLabel = state.progress?.totalBytes
    ? `${formatBytes(state.progress.downloadedBytes)} / ${formatBytes(state.progress.totalBytes)}`
    : state.progress
      ? `已下载 ${formatBytes(state.progress.downloadedBytes)}`
      : null;

  let icon = <Sparkles size={23} />;
  let title = "发现新版本";
  let description = "新版本可以在应用内安全下载并安装；请先等待正在运行的导出任务结束。";

  if (state.status === "checking") {
    icon = <LoaderCircle className="spin" size={23} />;
    title = "正在检查更新";
    description = "正在从更新服务器获取最新版本信息…";
  } else if (state.status === "up-to-date") {
    icon = <CheckCircle2 size={23} />;
    title = "当前已是最新版本";
    description = `企业微信问题雷达 v${currentVersion} 暂无可用更新。`;
  } else if (state.status === "downloading") {
    icon = <Download size={23} />;
    title = `正在下载 v${state.update?.version ?? ""}`;
    description = "下载完成后会先校验更新包签名，再开始安装。";
  } else if (state.status === "installing") {
    icon = <LoaderCircle className="spin" size={23} />;
    title = "正在安装更新";
    description = "应用将自动关闭并重新启动，请不要强制结束进程。";
  } else if (state.status === "restarting") {
    icon = <RotateCw className="spin" size={23} />;
    title = "更新已安装";
    description = "正在重新启动应用…";
  } else if (state.status === "restart-required") {
    icon = <RotateCw size={23} />;
    title = "请手动重启应用";
    description = state.error?.message ?? "更新已经安装，请关闭应用后重新打开。";
  } else if (state.status === "error") {
    icon = <ShieldAlert size={23} />;
    title = state.error?.kind === "signature"
      ? "安全校验未通过"
      : state.error?.kind === "busy"
        ? "请先等待任务完成"
        : "更新没有完成";
    description = state.error?.message ?? "更新失败，请稍后重试。";
  }

  return (
    <div className="modal-backdrop update-modal-backdrop">
      <section
        ref={dialogRef}
        className="modal-card update-modal"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        aria-labelledby="update-dialog-title"
        aria-describedby="update-dialog-description"
      >
        <div className="modal-icon update-modal-icon">{icon}</div>
        <h2 id="update-dialog-title">{title}</h2>
        <p id="update-dialog-description">{description}</p>

        {state.update && (
          <div className="update-version-row">
            <span>当前 v{state.update.currentVersion}</span>
            <span aria-hidden="true">→</span>
            <strong>新版 v{state.update.version}</strong>
            {releaseDate && <small>{releaseDate}</small>}
          </div>
        )}

        {state.update && ["available", "error"].includes(state.status) && (
          <div className="update-release-notes">
            <strong>本次更新</strong>
            <div>{state.update.body?.trim() || "此版本未提供更新说明。"}</div>
          </div>
        )}

        {state.status === "downloading" && state.progress && (
          <div className="update-progress" aria-live="polite">
            <div className="update-progress-heading">
              <span>{state.progress.percent === null ? "正在下载" : `${Math.round(state.progress.percent)}%`}</span>
              {progressLabel && <small>{progressLabel}</small>}
            </div>
            <div
              className="update-progress-rail"
              role="progressbar"
              aria-label="更新下载进度"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={state.progress.percent === null ? undefined : Math.round(state.progress.percent)}
            >
              <span
                className={state.progress.percent === null ? "update-progress-indeterminate" : ""}
                style={state.progress.percent === null ? undefined : { width: `${state.progress.percent}%` }}
              />
            </div>
          </div>
        )}

        {busy && state.status !== "downloading" && (
          <div className="update-busy-note" role="status" aria-live="polite">
            <LoaderCircle className="spin" size={14} />
            请保持应用运行
          </div>
        )}

        <div className="modal-actions update-modal-actions">
          {state.canDismiss && (
            <button className="button button-secondary" onClick={onDismiss}>
              {state.status === "available" ? "稍后更新" : "关闭"}
            </button>
          )}
          {state.status === "available" && (
            <button className="button button-primary" onClick={onInstall}>
              <Download size={14} />
              下载并安装
            </button>
          )}
          {state.status === "error" && (
            <button className="button button-primary" onClick={onRetry}>
              <RefreshCw size={14} />
              {state.error?.kind === "busy" ? "重试安装" : state.canInstall ? "重试更新" : "重新检查"}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}

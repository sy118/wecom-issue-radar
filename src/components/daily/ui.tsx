import "./daily.css";

import type { ReactNode } from "react";
import { AlertTriangle, Inbox, LoaderCircle, ShieldCheck } from "lucide-react";

import { highlightSegments, sourceLabel, statEntries } from "./logic";
import type { WeeklyStats } from "./types";

export function DailyPageHeader({
  eyebrow,
  icon,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  icon?: ReactNode;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-title-row">
      <div>
        <div className="eyebrow">
          {icon}
          {eyebrow}
        </div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="daily-head-actions">{actions}</div>}
    </div>
  );
}

export function DailyLoading({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className="daily-loading" role="status">
      <LoaderCircle className="spin" size={18} />
      <span>{label}</span>
    </div>
  );
}

export function DailyEmpty({
  title,
  body,
  icon,
  action,
}: {
  title: string;
  body: string;
  icon?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="daily-empty">
      {icon ?? <Inbox size={28} />}
      <strong>{title}</strong>
      <p>{body}</p>
      {action}
    </div>
  );
}

export function DailyError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="daily-error" role="alert">
      <AlertTriangle size={16} />
      <div>
        <span>{message}</span>
        {onRetry && (
          <button type="button" className="button button-secondary" style={{ marginLeft: 12 }} onClick={onRetry}>
            重试
          </button>
        )}
      </div>
    </div>
  );
}

export function DailyNote({ children }: { children: ReactNode }) {
  return (
    <div className="daily-note">
      <ShieldCheck size={16} />
      <span>{children}</span>
    </div>
  );
}

/** Renders plain text with highlighted search terms; never injects HTML. */
export function HighlightedText({ text, query }: { text: string; query: string }) {
  const segments = highlightSegments(text, query);
  return (
    <>
      {segments.map((segment, index) =>
        segment.match ? (
          <mark className="daily-mark" key={`${index}-${segment.text}`}>
            {segment.text}
          </mark>
        ) : (
          <span key={`${index}-${segment.text}`}>{segment.text}</span>
        ),
      )}
    </>
  );
}

export function SourceBadge({ source }: { source?: string }) {
  const tone = source === "wecom" ? "wecom" : source === "wechat" ? "wechat" : "muted";
  return <span className={`daily-badge daily-badge-${tone}`}>{sourceLabel(source)}</span>;
}

export function StatGrid({ stats }: { stats: WeeklyStats }) {
  const entries = statEntries(stats);
  if (!entries.length) return null;
  return (
    <div className="daily-stats" aria-label="统计">
      {entries.map((entry) => (
        <div className="daily-stat" key={entry.key}>
          <strong>{entry.value}</strong>
          <span>{entry.label}</span>
        </div>
      ))}
    </div>
  );
}

/** Clipboard write with a legacy textarea fallback for non-secure contexts. */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false;
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Electron may deny the async clipboard in a sandboxed renderer.
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    return copied;
  } catch {
    return false;
  }
}

/** Triggers a local Markdown download (Electron renderer or browser preview). */
export function downloadMarkdown(filename: string, content: string): boolean {
  try {
    const name = filename.endsWith(".md") ? filename : `${filename}.md`;
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    return true;
  } catch {
    return false;
  }
}

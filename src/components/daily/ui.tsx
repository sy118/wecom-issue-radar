import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, LoaderCircle, ShieldCheck, Sparkles } from "lucide-react";

import { Button } from "../ui";

/** Shared chrome for the three 沟通助手 pages. */
export function DailyPageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-title-row">
      <div>
        <div className="eyebrow">
          <Sparkles size={13} />
          {eyebrow}
        </div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="dl-head-actions">{actions}</div>}
    </div>
  );
}

/** Muted inline hint; `tone="warning"` is used for AI fallback results. */
export function DailyNotice({
  tone = "muted",
  icon,
  children,
}: {
  tone?: "muted" | "warning" | "error" | "success";
  icon?: ReactNode;
  children: ReactNode;
}) {
  const glyph =
    icon
    ?? (tone === "warning" || tone === "error" ? <AlertTriangle size={16} /> : tone === "success" ? <CheckCircle2 size={16} /> : <ShieldCheck size={16} />);
  return (
    <div className={`dl-notice dl-notice-${tone}`} role={tone === "error" ? "alert" : undefined}>
      <span className="dl-notice-icon">{glyph}</span>
      <div className="dl-notice-body">{children}</div>
    </div>
  );
}

export function DailyBusy({ label }: { label: string }) {
  return (
    <span className="dl-busy" role="status">
      <LoaderCircle className="spin" size={15} />
      {label}
    </span>
  );
}

export function DailyError({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <DailyNotice tone="error">
      <span>{message}</span>
      {onRetry && (
        <Button variant="secondary" className="dl-inline-button" onClick={onRetry}>
          重试
        </Button>
      )}
    </DailyNotice>
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

/** Small labelled block used by the interpretation and check panels. */
export function DailyFieldset({
  title,
  meta,
  children,
}: {
  title: string;
  meta?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="dl-fieldset">
      <div className="dl-fieldset-head">
        <h3>{title}</h3>
        {meta}
      </div>
      {children}
    </div>
  );
}

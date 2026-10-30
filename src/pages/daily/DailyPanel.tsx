import { useId, type ReactNode } from "react";

/**
 * Shared section shell for the three 沟通助手 pages: one title, one optional
 * description, one right-aligned action cluster, then the section body.
 * Keeping the markup here keeps the input/result hierarchy identical across
 * reply / phrase / digest and lets daily.css stay layout-only.
 */
export function DailyPanel({
  title,
  description,
  actions,
  children,
  className,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const titleId = useId();
  return (
    <section
      className={className ? `glass-card dl-panel ${className}` : "glass-card dl-panel"}
      aria-labelledby={titleId}
    >
      <div className="dl-panel-head">
        <div className="dl-panel-title">
          <h2 id={titleId}>{title}</h2>
          {description && <p>{description}</p>}
        </div>
        {actions && <div className="dl-panel-actions">{actions}</div>}
      </div>
      <div className="dl-panel-body">{children}</div>
    </section>
  );
}

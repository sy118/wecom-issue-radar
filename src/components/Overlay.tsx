import { useId, type ReactNode } from "react";
import { createPortal } from "react-dom";
import clsx from "clsx";
import { X } from "lucide-react";

import { useDialogA11y } from "../lib/useDialogA11y";

/** Shared dialog behaviour lives in one module (focus trap, stack, Esc, locks). */
export {
  OVERLAY_FOCUSABLE_SELECTOR,
  nextFocusIndex,
  createBodyScrollLock,
  bodyScrollLock,
  pageScrollLock,
} from "../lib/useDialogA11y";
export type { BodyScrollLockTarget } from "../lib/useDialogA11y";

/**
 * Single application overlay primitive. Renders through a portal on document.body
 * so dialogs are never constrained by page containers, transforms or nested
 * overflow. The page keeps the only scroll container; opening an overlay locks
 * body scroll once (reference counted) so nested dialogs cannot fight over it.
 * Keyboard/focus behaviour (trap, Escape, initial focus, focus restore) is
 * provided by `useDialogA11y`, which the drawers share as well.
 */

export type OverlaySize = "sm" | "md" | "lg" | "xl";

export function Overlay({
  open,
  onClose,
  title,
  subtitle,
  eyebrow,
  ariaLabel,
  size = "lg",
  footer,
  footerClassName,
  children,
  className,
  closeOnBackdrop = true,
  closeOnEscape = true,
  closeDisabled = false,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  subtitle?: ReactNode;
  eyebrow?: ReactNode;
  ariaLabel?: string;
  size?: OverlaySize;
  footer?: ReactNode;
  footerClassName?: string;
  children: ReactNode;
  className?: string;
  closeOnBackdrop?: boolean;
  closeOnEscape?: boolean;
  /** Disable close affordances while a save/sync request is in flight. */
  closeDisabled?: boolean;
}) {
  const panelRef = useDialogA11y<HTMLElement>({
    open,
    onClose,
    closeOnEscape,
    closeDisabled,
    initialFocusWithin: ".overlay-body",
  });
  const headingId = useId();
  const titleId = title ? headingId : undefined;
  const hasHeading = Boolean(title || subtitle || eyebrow);

  if (!open) return null;

  const close = () => {
    if (!closeDisabled) onClose();
  };

  return createPortal(
    <div className={clsx("overlay-root", className)} data-size={size}>
      <div
        className="overlay-backdrop"
        aria-hidden="true"
        onMouseDown={(event) => {
          if (closeOnBackdrop && !closeDisabled && event.target === event.currentTarget) {
            onClose();
          }
        }}
      />
      <section
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title ? undefined : ariaLabel || "对话框"}
        aria-labelledby={titleId}
        tabIndex={-1}
        className={clsx("overlay-panel", `is-${size}`)}
      >
        {hasHeading ? (
          <header className="overlay-header">
            <div className="overlay-heading">
              {eyebrow && <span className="overlay-eyebrow">{eyebrow}</span>}
              {title && <h2 id={titleId}>{title}</h2>}
              {subtitle && <p>{subtitle}</p>}
            </div>
            <button
              type="button"
              className="overlay-close"
              aria-label="关闭"
              disabled={closeDisabled}
              onClick={close}
            >
              <X size={18} />
            </button>
          </header>
        ) : (
          <button
            type="button"
            className="overlay-close overlay-close-floating"
            aria-label="关闭"
            disabled={closeDisabled}
            onClick={close}
          >
            <X size={18} />
          </button>
        )}
        <div className="overlay-body">{children}</div>
        {footer && <footer className={clsx("overlay-footer", footerClassName)}>{footer}</footer>}
      </section>
    </div>,
    document.body,
  );
}

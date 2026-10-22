import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import clsx from "clsx";
import { X } from "lucide-react";

/**
 * Single application overlay primitive. Renders through a portal on document.body
 * so dialogs are never constrained by page containers, transforms or nested
 * overflow. The page keeps the only scroll container; opening an overlay locks
 * body scroll once (reference counted) so nested dialogs cannot fight over it.
 */

export const OVERLAY_FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[contenteditable='true']",
  "audio[controls]",
  "video[controls]",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export type OverlaySize = "sm" | "md" | "lg" | "xl";

export interface BodyScrollLockTarget {
  style: { overflow: string; paddingRight: string };
  clientWidth: number;
}

/** Reference-counted body scroll lock shared by every overlay instance. */
export function createBodyScrollLock(options: { getViewportWidth?: () => number } = {}) {
  let depth = 0;
  let previousOverflow = "";
  let previousPaddingRight = "";
  const getViewportWidth = options.getViewportWidth
    ?? (() => (typeof window === "undefined" ? 0 : window.innerWidth));

  return {
    get active() {
      return depth > 0;
    },
    acquire(body: BodyScrollLockTarget): () => void {
      if (depth === 0) {
        previousOverflow = body.style.overflow;
        previousPaddingRight = body.style.paddingRight;
        const scrollbarGap = Math.max(0, getViewportWidth() - body.clientWidth);
        body.style.overflow = "hidden";
        if (scrollbarGap > 0) {
          const current = Number.parseFloat(previousPaddingRight) || 0;
          body.style.paddingRight = `${current + scrollbarGap}px`;
        }
      }
      depth += 1;
      let released = false;
      return () => {
        if (released) return;
        released = true;
        depth = Math.max(0, depth - 1);
        if (depth === 0) {
          body.style.overflow = previousOverflow;
          body.style.paddingRight = previousPaddingRight;
        }
      };
    },
  };
}

const bodyScrollLock = createBodyScrollLock();

/** Wraps the focus ring for a focusable list. Exported for unit tests. */
export function nextFocusIndex(count: number, currentIndex: number, shiftKey: boolean): number {
  if (count <= 0) return -1;
  if (currentIndex < 0 || currentIndex >= count) return shiftKey ? count - 1 : 0;
  if (shiftKey) return currentIndex === 0 ? count - 1 : currentIndex - 1;
  return currentIndex === count - 1 ? 0 : currentIndex + 1;
}

function focusableElements(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(OVERLAY_FOCUSABLE_SELECTOR)).filter(
    (node) =>
      !node.hasAttribute("hidden")
      && node.getAttribute("aria-hidden") !== "true"
      && (node.offsetParent !== null || node.getClientRects().length > 0),
  );
}

let overlaySequence = 0;
const overlayStack: number[] = [];
let pageScrollLockDepth = 0;

function lockPageScroll(): () => void {
  if (pageScrollLockDepth === 0) {
    document.documentElement.classList.add("overlay-open");
  }
  pageScrollLockDepth += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    pageScrollLockDepth = Math.max(0, pageScrollLockDepth - 1);
    if (pageScrollLockDepth === 0) {
      document.documentElement.classList.remove("overlay-open");
    }
  };
}

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
  const panelRef = useRef<HTMLElement | null>(null);
  const previousFocus = useRef<Element | null>(null);
  const onCloseRef = useRef(onClose);
  const closeDisabledRef = useRef(closeDisabled);
  onCloseRef.current = onClose;
  closeDisabledRef.current = closeDisabled;
  const headingId = useId();
  const titleId = title ? headingId : undefined;
  const hasHeading = Boolean(title || subtitle || eyebrow);

  useEffect(() => {
    if (!open) return;
    const overlayId = ++overlaySequence;
    overlayStack.push(overlayId);
    previousFocus.current = document.activeElement;
    const releaseScrollLock = bodyScrollLock.acquire(document.body);
    const releasePageScroll = lockPageScroll();
    const frame = window.requestAnimationFrame(() => {
      const panel = panelRef.current;
      if (!panel) return;
      const explicit = panel.querySelector<HTMLElement>("[data-autofocus]");
      const body = panel.querySelector<HTMLElement>(".overlay-body");
      const closeButton = panel.querySelector<HTMLElement>(".overlay-close");
      const focusable = focusableElements(body ?? panel);
      (explicit ?? focusable[0] ?? closeButton ?? panel).focus();
    });

    const onKeyDown = (event: KeyboardEvent) => {
      if (overlayStack[overlayStack.length - 1] !== overlayId) return;
      if (event.key === "Escape" && closeOnEscape) {
        event.preventDefault();
        event.stopPropagation();
        if (!closeDisabledRef.current) onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const nodes = focusableElements(panel);
      if (nodes.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const currentIndex = nodes.indexOf(document.activeElement as HTMLElement);
      const targetIndex = nextFocusIndex(nodes.length, currentIndex, event.shiftKey);
      if (
        (event.shiftKey && currentIndex <= 0)
        || (!event.shiftKey && currentIndex === nodes.length - 1)
        || currentIndex < 0
      ) {
        event.preventDefault();
        nodes[Math.max(0, targetIndex)]?.focus();
      }
    };

    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKeyDown, true);
      releaseScrollLock();
      releasePageScroll();
      const stackIndex = overlayStack.lastIndexOf(overlayId);
      if (stackIndex >= 0) overlayStack.splice(stackIndex, 1);
      const target = previousFocus.current;
      if (target instanceof HTMLElement && target.isConnected) target.focus();
    };
  }, [open, closeOnEscape]);

  if (!open) return null;

  const close = () => {
    if (!closeDisabledRef.current) onCloseRef.current();
  };

  return createPortal(
    <div className={clsx("overlay-root", className)} data-size={size}>
      <div
        className="overlay-backdrop"
        aria-hidden="true"
        onMouseDown={(event) => {
          if (closeOnBackdrop && !closeDisabledRef.current && event.target === event.currentTarget) {
            onCloseRef.current();
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

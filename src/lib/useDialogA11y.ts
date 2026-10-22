import { useEffect, useRef, type RefObject } from "react";

/**
 * Native-dialog keyboard/focus behaviour shared by every dialog layer:
 * drawers, modal cards and the portal `Overlay`.
 *
 * One implementation covers initial focus, the focus trap (Tab / Shift+Tab),
 * Escape-to-close (disableable while saving), a nested-dialog stack where only
 * the topmost layer reacts, focus restoration on close/unmount, and the page
 * scroll lock (`html.overlay-open`) that keeps the workspace behind a dialog
 * from scrolling. Keeping it here avoids the drift that came from per-drawer
 * copies and keeps the page-scroll lock reference-counted across layers.
 */

export const OVERLAY_FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  "[contenteditable='true']",
  "audio[controls]",
  "video[controls]",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

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

/**
 * Reference-counted page scroll lock. `html.overlay-open` freezes both the
 * document and the Electron shell's `.page-frame` scroller (see overlay.css),
 * so a dialog never scrolls the workspace behind it. Nested layers share the
 * same counter and only the last release restores scrolling.
 */
export function createPageScrollLock() {
  let depth = 0;
  return {
    get active() {
      return depth > 0;
    },
    acquire(): () => void {
      if (depth === 0) {
        document.documentElement.classList.add("overlay-open");
      }
      depth += 1;
      let released = false;
      return () => {
        if (released) return;
        released = true;
        depth = Math.max(0, depth - 1);
        if (depth === 0) {
          document.documentElement.classList.remove("overlay-open");
        }
      };
    },
  };
}

/** The single body/page scroll lock pair used by every dialog layer. */
export const bodyScrollLock = createBodyScrollLock();
export const pageScrollLock = createPageScrollLock();

/** Wraps the focus ring for a focusable list. Exported for unit tests. */
export function nextFocusIndex(count: number, currentIndex: number, shiftKey: boolean): number {
  if (count <= 0) return -1;
  if (currentIndex < 0 || currentIndex >= count) return shiftKey ? count - 1 : 0;
  if (shiftKey) return currentIndex === 0 ? count - 1 : currentIndex - 1;
  return currentIndex === count - 1 ? 0 : currentIndex + 1;
}

/** Every element the trap may focus, in DOM order, skipping hidden nodes. */
export function getFocusableElements(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(OVERLAY_FOCUSABLE_SELECTOR)).filter(
    (node) =>
      !node.hasAttribute("hidden")
      && node.getAttribute("aria-hidden") !== "true"
      && (node.offsetParent !== null || node.getClientRects().length > 0),
  );
}

/** Open dialogs, oldest first. Only the last id reacts to Escape/Tab. */
export const overlayStack: number[] = [];
let overlaySequence = 0;

function focusInitial(panel: HTMLElement, mode: "first" | "panel", within?: string) {
  if (mode === "panel") {
    panel.focus();
    return;
  }
  const explicit = panel.querySelector<HTMLElement>("[data-autofocus]");
  const scope = within ? panel.querySelector<HTMLElement>(within) : null;
  const candidates = scope && scope !== panel ? getFocusableElements(scope) : getFocusableElements(panel);
  const target = explicit ?? candidates[0] ?? panel;
  target.focus();
}

export interface UseDialogA11yOptions {
  /** The dialog is rendered/open. Effects only run while true. */
  open: boolean;
  /** Invoked for Escape and for host-provided close affordances. */
  onClose?: () => void;
  /** Escape closes the dialog. Defaults to true. */
  closeOnEscape?: boolean;
  /** Escape is swallowed while true (saving/exporting/busy). */
  closeDisabled?: boolean;
  /** `"first"` focuses `[data-autofocus]`/first focusable; `"panel"` the panel. */
  initialFocus?: "first" | "panel";
  /** Optional container whose first focusable element receives initial focus. */
  initialFocusWithin?: string;
  /** Lock body + page scrolling while open. Defaults to true. */
  lockScroll?: boolean;
  /** Restore the pre-open focus target on close/unmount. Defaults to true. */
  restoreFocus?: boolean;
}

/**
 * Attach the returned ref to the dialog panel (`tabIndex={-1}`), and the hook
 * takes care of the rest. The listener lives on `window` in the capture phase
 * so page-level handlers (and background dialogs) never see the keys first.
 */
export function useDialogA11y<T extends HTMLElement = HTMLElement>(
  options: UseDialogA11yOptions,
): RefObject<T> {
  const {
    open,
    closeOnEscape = true,
    closeDisabled = false,
    initialFocus = "first",
    initialFocusWithin,
    lockScroll = true,
    restoreFocus = true,
  } = options;
  const panelRef = useRef<T>(null);
  const previousFocus = useRef<Element | null>(null);
  const openRef = useRef(false);
  const onCloseRef = useRef(options.onClose);
  const closeDisabledRef = useRef(closeDisabled);
  const initialFocusRef = useRef(initialFocus);
  const initialFocusWithinRef = useRef(initialFocusWithin);
  onCloseRef.current = options.onClose;
  closeDisabledRef.current = closeDisabled;
  initialFocusRef.current = initialFocus;
  initialFocusWithinRef.current = initialFocusWithin;

  // Capture the trigger during render, before commit-phase focus effects (e.g.
  // a child `autoFocus`) can replace `document.activeElement`. Falls back to
  // the effect-time active element if this dialog never rendered a capture.
  if (open !== openRef.current) {
    openRef.current = open;
    if (open && typeof document !== "undefined") {
      previousFocus.current = document.activeElement;
    }
  }

  // If the control that owns focus becomes disabled mid-save (save buttons are
  // disabled while a request is in flight) the browser blurs it to <body>.
  // Pull focus back to the panel so keyboard users keep their dialog context.
  useEffect(() => {
    if (!open || !closeDisabled) return;
    const panel = panelRef.current;
    if (!panel) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement && panel.contains(active)) return;
    const frame = window.requestAnimationFrame(() => {
      const current = panelRef.current;
      if (!current) return;
      const activeNow = document.activeElement;
      if (activeNow instanceof HTMLElement && current.contains(activeNow)) return;
      current.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [open, closeDisabled]);

  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    if (!panel) return;

    const dialogId = ++overlaySequence;
    overlayStack.push(dialogId);
    if (!previousFocus.current) previousFocus.current = document.activeElement;
    const releaseBodyScroll = lockScroll ? bodyScrollLock.acquire(document.body) : undefined;
    const releasePageScroll = lockScroll ? pageScrollLock.acquire() : undefined;
    const frame = window.requestAnimationFrame(() => {
      const current = panelRef.current;
      if (current) focusInitial(current, initialFocusRef.current, initialFocusWithinRef.current);
    });

    const onKeyDown = (event: KeyboardEvent) => {
      if (overlayStack[overlayStack.length - 1] !== dialogId) return;
      if (event.key === "Escape" && closeOnEscape) {
        event.preventDefault();
        event.stopPropagation();
        if (!closeDisabledRef.current) onCloseRef.current?.();
        return;
      }
      if (event.key !== "Tab") return;
      const current = panelRef.current;
      if (!current) return;
      const nodes = getFocusableElements(current);
      if (nodes.length === 0) {
        event.preventDefault();
        current.focus();
        return;
      }
      const active = document.activeElement;
      const currentIndex = active instanceof HTMLElement ? nodes.indexOf(active) : -1;
      if (currentIndex < 0) {
        // Focus escaped the panel, or sits on a natively focusable control the
        // query does not list; only recapture when it is outside the panel.
        if (active instanceof HTMLElement && current.contains(active)) return;
        event.preventDefault();
        (event.shiftKey ? nodes[nodes.length - 1] : nodes[0])?.focus();
        return;
      }
      const shouldWrap = event.shiftKey
        ? currentIndex === 0
        : currentIndex === nodes.length - 1;
      if (!shouldWrap) return;
      event.preventDefault();
      const targetIndex = nextFocusIndex(nodes.length, currentIndex, event.shiftKey);
      nodes[Math.max(0, targetIndex)]?.focus();
    };

    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKeyDown, true);
      releaseBodyScroll?.();
      releasePageScroll?.();
      const stackIndex = overlayStack.lastIndexOf(dialogId);
      if (stackIndex >= 0) overlayStack.splice(stackIndex, 1);
      const target = previousFocus.current;
      previousFocus.current = null;
      if (restoreFocus && target instanceof HTMLElement && target.isConnected) target.focus();
    };
    // closeOnEscape/lockScroll/restoreFocus are stable literals at call sites;
    // only `open` should re-register the layer. Latest callbacks stay in refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, closeOnEscape, lockScroll, restoreFocus]);

  return panelRef;
}

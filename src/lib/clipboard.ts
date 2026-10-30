/**
 * Clipboard write that works in every desktop context.
 *
 * Electron's packaged renderer is loaded from ``file://``; there the async
 * Clipboard API can be unavailable or reject when the window is not focused.
 * The main process owns the OS clipboard, so it is tried first, then the
 * renderer APIs, and finally the legacy textarea + ``execCommand`` fallback.
 */

import { bridge } from "./bridge";
import { hasDesktop } from "./desktop";

const CLIPBOARD_ERROR = "无法写入剪贴板";

export async function copyToClipboard(text: string): Promise<void> {
  if (!text) return;

  if (hasDesktop()) {
    try {
      const written = await bridge.writeClipboard(text);
      if (written === true) return;
    } catch {
      // Fall through to the renderer implementations.
    }
  }

  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Unfocused windows and file:// origins may reject.
    }
  }

  if (typeof document === "undefined") throw new Error(CLIPBOARD_ERROR);

  const active = document.activeElement as HTMLElement | null;
  const previous = active && typeof active.focus === "function" ? active : null;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.opacity = "0";
  textarea.style.pointerEvents = "none";
  document.body.appendChild(textarea);
  try {
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    const copied = typeof document.execCommand === "function" && document.execCommand("copy");
    if (!copied) throw new Error(CLIPBOARD_ERROR);
  } finally {
    textarea.remove();
    if (previous && typeof previous.focus === "function") previous.focus();
  }
}

/**
 * Clipboard helper that works in Electron's file:// renderer, where the async
 * Clipboard API can be unavailable or denied. Falls back to a hidden textarea
 * plus `document.execCommand("copy")`.
 */
export async function copyToClipboard(text: string): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // fall through to the DOM fallback
    }
  }
  if (typeof document === "undefined") throw new Error("当前环境不支持剪贴板");
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  textarea.style.pointerEvents = "none";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  if (!copied) throw new Error("无法写入剪贴板");
}

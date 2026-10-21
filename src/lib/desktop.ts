import type { DesktopApi } from "../../electron/shared/contracts";

declare global {
  interface Window {
    desktop?: DesktopApi;
  }
}

/**
 * Returns the preload bridge. Throws when the renderer is opened outside the
 * desktop shell (for example a plain browser during component tests).
 *
 * The bridge is read from `globalThis` rather than `window` so unit tests can
 * stub it in a Node environment.
 */
export function getDesktop(): DesktopApi {
  const api = (globalThis as { desktop?: DesktopApi }).desktop;
  if (!api) {
    throw new Error("桌面桥接不可用，请从桌面应用启动。");
  }
  return api;
}

export function hasDesktop(): boolean {
  return Boolean((globalThis as { desktop?: DesktopApi }).desktop);
}

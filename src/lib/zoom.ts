import type { DesktopApi } from "../../electron/shared/contracts";

/**
 * Renderer-side interface zoom helper.
 *
 * The Electron main process owns the real `webContents` zoom and persists it in
 * `<dataRoot>/ui-state.json`. This module is the single place the renderer talks
 * to that service, with localStorage as the fallback for browser previews and
 * for recovering the last user choice when the bridge is unavailable.
 */

export const ZOOM_LEVELS = [1, 1.1, 1.25] as const;
export const MIN_ZOOM = 0.8;
export const MAX_ZOOM = 1.5;
export const DEFAULT_ZOOM = 1;
export const ZOOM_STORAGE_KEY = "issue-radar-zoom";

function desktopApi(): DesktopApi | undefined {
  return (globalThis as { desktop?: DesktopApi }).desktop;
}

function getStorage(): Storage | null {
  try {
    return (globalThis as { localStorage?: Storage }).localStorage ?? null;
  } catch {
    return null;
  }
}

function isInRange(value: number): boolean {
  return Number.isFinite(value) && value >= MIN_ZOOM && value <= MAX_ZOOM;
}

/** Clamps any requested factor into the supported 0.8–1.5 range. */
export function normalizeZoom(factor: number): number {
  if (!Number.isFinite(factor)) return DEFAULT_ZOOM;
  const clamped = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, factor));
  return Math.round(clamped * 100) / 100;
}

/** Reads the last zoom saved in localStorage; invalid values fall back to 100%. */
export function getSavedZoom(): number {
  const storage = getStorage();
  if (!storage) return DEFAULT_ZOOM;
  let raw: string | null = null;
  try {
    raw = storage.getItem(ZOOM_STORAGE_KEY);
  } catch {
    return DEFAULT_ZOOM;
  }
  if (raw === null || raw.trim() === "") return DEFAULT_ZOOM;
  const parsed = Number(raw);
  return isInRange(parsed) ? Math.round(parsed * 100) / 100 : DEFAULT_ZOOM;
}

function saveZoom(factor: number): void {
  const storage = getStorage();
  if (!storage) return;
  try {
    storage.setItem(ZOOM_STORAGE_KEY, String(factor));
  } catch {
    // A blocked/full localStorage must never break the settings page.
  }
}

/**
 * Applies a zoom factor through the desktop bridge when available.
 *
 * Bridge failures are deliberately swallowed: the settings page awaits this
 * function and should not surface an error just because the native zoom call
 * failed. The value is always persisted to localStorage so the choice survives
 * a reload, and the normalized factor is returned.
 */
export async function applyZoom(factor: number): Promise<number> {
  const zoom = normalizeZoom(factor);
  const desktop = desktopApi();
  if (desktop) {
    try {
      await desktop.invoke("zoom_set", { factor: zoom });
    } catch {
      // Keep the user's choice even when the main process is unavailable.
    }
  }
  saveZoom(zoom);
  return zoom;
}

/**
 * Restores the zoom saved by the main process (authoritative) or localStorage
 * (fallback) and applies it. Safe to call once at startup; it never rejects.
 */
export async function loadAndApplyZoom(): Promise<number> {
  const fallback = getSavedZoom();
  const desktop = desktopApi();
  if (!desktop) return fallback;
  try {
    const persisted = await desktop.invoke<number>("zoom_get");
    const zoom = isInRange(persisted) ? normalizeZoom(persisted) : fallback;
    return await applyZoom(zoom);
  } catch {
    return applyZoom(fallback);
  }
}

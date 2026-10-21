/** UI zoom persistence (`<dataRoot>/ui-state.json`, no extra dependency). */

import path from "node:path";

import { atomicWriteJson, readJsonFile } from "./files";
import { isRecord } from "./utils";

export const MIN_ZOOM = 0.8;
export const MAX_ZOOM = 1.5;
export const DEFAULT_ZOOM = 1;

/** Returns a usable zoom factor, falling back to 100% for missing/invalid data. */
export function normalizeZoom(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return DEFAULT_ZOOM;
  if (value < MIN_ZOOM || value > MAX_ZOOM) return DEFAULT_ZOOM;
  return Math.round(value * 100) / 100;
}

/** Validates a requested zoom factor (exported for tests). */
export function validateZoomFactor(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error("界面缩放比例无效");
  }
  if (value < MIN_ZOOM || value > MAX_ZOOM) {
    throw new Error(
      `界面缩放比例必须在 ${MIN_ZOOM.toFixed(1)} 到 ${MAX_ZOOM.toFixed(1)} 之间`,
    );
  }
  return Math.round(value * 100) / 100;
}

export class ZoomService {
  private readonly statePath: string;
  private cached: number | null = null;

  constructor(dataRoot: string) {
    this.statePath = path.join(dataRoot, "ui-state.json");
  }

  async getZoom(): Promise<number> {
    if (this.cached !== null) return this.cached;
    const state = await this.readState();
    this.cached = normalizeZoom(state.zoom);
    return this.cached;
  }

  async setZoom(factor: number): Promise<void> {
    const zoom = validateZoomFactor(factor);
    const state = await this.readState();
    await atomicWriteJson(this.statePath, { ...state, zoom });
    this.cached = zoom;
  }

  private async readState(): Promise<Record<string, unknown>> {
    try {
      const value = await readJsonFile<unknown>(this.statePath);
      return isRecord(value) ? value : {};
    } catch {
      // Corrupt UI state must never block startup; fall back to defaults.
      return {};
    }
  }
}

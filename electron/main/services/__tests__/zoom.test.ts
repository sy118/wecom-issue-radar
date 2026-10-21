import { describe, expect, it } from "vitest";

import { DEFAULT_ZOOM, normalizeZoom, validateZoomFactor } from "../zoom";

describe("ui zoom persistence helpers", () => {
  it("normalizes missing or out-of-range values to 100%", () => {
    expect(normalizeZoom(undefined)).toBe(DEFAULT_ZOOM);
    expect(normalizeZoom("1.25")).toBe(DEFAULT_ZOOM);
    expect(normalizeZoom(0.5)).toBe(DEFAULT_ZOOM);
    expect(normalizeZoom(2)).toBe(DEFAULT_ZOOM);
    expect(normalizeZoom(1.1)).toBe(1.1);
    expect(normalizeZoom(1.333)).toBe(1.33);
  });

  it("validates the 0.8–1.5 range", () => {
    expect(validateZoomFactor(0.8)).toBe(0.8);
    expect(validateZoomFactor(1.5)).toBe(1.5);
    expect(() => validateZoomFactor(0.79)).toThrow("0.8 到 1.5");
    expect(() => validateZoomFactor(1.51)).toThrow("0.8 到 1.5");
    expect(() => validateZoomFactor(Number.NaN)).toThrow("无效");
  });
});

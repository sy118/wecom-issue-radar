import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyZoom,
  DEFAULT_ZOOM,
  getSavedZoom,
  loadAndApplyZoom,
  normalizeZoom,
  ZOOM_LEVELS,
  ZOOM_STORAGE_KEY,
} from "./zoom";

function fakeStorage(): Storage {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => (data.has(key) ? (data.get(key) as string) : null),
    setItem: (key: string, value: string) => {
      data.set(key, String(value));
    },
    removeItem: (key: string) => {
      data.delete(key);
    },
    clear: () => data.clear(),
    key: (index: number) => [...data.keys()][index] ?? null,
    get length() {
      return data.size;
    },
  } as unknown as Storage;
}

function stubDesktop(invoke: ReturnType<typeof vi.fn>): void {
  vi.stubGlobal("desktop", {
    platform: "win32",
    versions: { app: "4.0.0", electron: "45.0.0", chrome: "140", node: "22" },
    invoke,
    on: () => () => {},
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("zoom preferences", () => {
  it("exposes the three supported UI sizes", () => {
    expect(ZOOM_LEVELS).toEqual([1, 1.1, 1.25]);
  });

  it("clamps requested factors into the supported range", () => {
    expect(normalizeZoom(0.5)).toBe(0.8);
    expect(normalizeZoom(2)).toBe(1.5);
    expect(normalizeZoom(1.234)).toBe(1.23);
    expect(normalizeZoom(Number.NaN)).toBe(DEFAULT_ZOOM);
    expect(normalizeZoom(Number.POSITIVE_INFINITY)).toBe(DEFAULT_ZOOM);
    expect(normalizeZoom(1.1)).toBe(1.1);
  });

  it("reads a valid saved zoom and falls back to 100% for missing or invalid data", () => {
    const storage = fakeStorage();
    vi.stubGlobal("localStorage", storage);

    expect(getSavedZoom()).toBe(1);
    storage.setItem(ZOOM_STORAGE_KEY, "1.25");
    expect(getSavedZoom()).toBe(1.25);
    storage.setItem(ZOOM_STORAGE_KEY, "abc");
    expect(getSavedZoom()).toBe(1);
    storage.setItem(ZOOM_STORAGE_KEY, "3");
    expect(getSavedZoom()).toBe(1);
    storage.setItem(ZOOM_STORAGE_KEY, "0.9");
    expect(getSavedZoom()).toBe(0.9);
  });

  it("persists the factor and forwards it to the desktop bridge", async () => {
    const storage = fakeStorage();
    const invoke = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("localStorage", storage);
    stubDesktop(invoke);

    await expect(applyZoom(1.1)).resolves.toBe(1.1);
    expect(invoke).toHaveBeenCalledWith("zoom_set", { factor: 1.1 });
    expect(storage.getItem(ZOOM_STORAGE_KEY)).toBe("1.1");
  });

  it("keeps the choice without a bridge and never rejects on bridge failure", async () => {
    const storage = fakeStorage();
    vi.stubGlobal("localStorage", storage);
    await expect(applyZoom(2)).resolves.toBe(1.5);
    expect(storage.getItem(ZOOM_STORAGE_KEY)).toBe("1.5");

    const invoke = vi.fn().mockRejectedValue(new Error("main process gone"));
    stubDesktop(invoke);
    await expect(applyZoom(1.25)).resolves.toBe(1.25);
    expect(storage.getItem(ZOOM_STORAGE_KEY)).toBe("1.25");
  });

  it("restores the zoom persisted by the main process on startup", async () => {
    const storage = fakeStorage();
    const invoke = vi.fn()
      .mockResolvedValueOnce(1.25) // zoom_get
      .mockResolvedValueOnce(undefined); // zoom_set
    vi.stubGlobal("localStorage", storage);
    stubDesktop(invoke);

    await expect(loadAndApplyZoom()).resolves.toBe(1.25);
    expect(invoke).toHaveBeenNthCalledWith(1, "zoom_get");
    expect(invoke).toHaveBeenNthCalledWith(2, "zoom_set", { factor: 1.25 });
    expect(storage.getItem(ZOOM_STORAGE_KEY)).toBe("1.25");
  });

  it("falls back to the saved local value when the main process cannot answer", async () => {
    const storage = fakeStorage();
    storage.setItem(ZOOM_STORAGE_KEY, "1.1");
    const invoke = vi.fn()
      .mockRejectedValueOnce(new Error("zoom_get failed"))
      .mockResolvedValueOnce(undefined);
    vi.stubGlobal("localStorage", storage);
    stubDesktop(invoke);

    await expect(loadAndApplyZoom()).resolves.toBe(1.1);
    expect(invoke).toHaveBeenNthCalledWith(1, "zoom_get");
    expect(invoke).toHaveBeenNthCalledWith(2, "zoom_set", { factor: 1.1 });
  });
});

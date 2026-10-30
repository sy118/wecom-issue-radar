import { afterEach, describe, expect, it, vi } from "vitest";

import { copyToClipboard } from "./clipboard";
import type { DesktopApi } from "../../electron/shared/contracts";

function desktop(invoke: (command: string, payload?: unknown) => Promise<unknown>): DesktopApi {
  return {
    platform: "win32",
    versions: { app: "4.5.1", electron: "45.0.0", chrome: "140", node: "22" },
    invoke: invoke as DesktopApi["invoke"],
    on: () => () => {},
  };
}

function stubDesktop(invoke?: (command: string, payload?: unknown) => Promise<unknown>): void {
  if (invoke) vi.stubGlobal("desktop", desktop(invoke));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("copyToClipboard", () => {
  it("prefers the main-process clipboard and skips renderer APIs", async () => {
    const invoke = vi.fn(async () => true);
    stubDesktop(invoke);
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await copyToClipboard("草稿正文");

    expect(invoke).toHaveBeenCalledWith("clipboard_write_text", { text: "草稿正文" });
    expect(writeText).not.toHaveBeenCalled();
  });

  it("falls back to the async Clipboard API when the bridge call fails", async () => {
    const invoke = vi.fn(async () => {
      throw new Error("bridge unavailable");
    });
    stubDesktop(invoke);
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await copyToClipboard("正文");

    expect(writeText).toHaveBeenCalledWith("正文");
  });

  it("uses the textarea fallback and restores focus when renderer APIs fail", async () => {
    const writeText = vi.fn(async () => {
      throw new Error("denied");
    });
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const focused = vi.fn();
    const previous = { focus: focused } as unknown as HTMLElement;
    const remove = vi.fn();
    const textarea = {
      value: "",
      style: {} as CSSStyleDeclaration,
      setAttribute: vi.fn(),
      focus: vi.fn(),
      select: vi.fn(),
      setSelectionRange: vi.fn(),
      remove,
    };
    const execCommand = vi.fn(() => true);
    vi.stubGlobal("document", {
      activeElement: previous,
      createElement: vi.fn(() => textarea),
      body: { appendChild: vi.fn() },
      execCommand,
    });

    await copyToClipboard("手动复制内容");

    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(textarea.value).toBe("手动复制内容");
    expect(remove).toHaveBeenCalled();
    expect(focused).toHaveBeenCalled();
  });

  it("rejects when no clipboard path is available", async () => {
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("document", {
      activeElement: null,
      createElement: () => ({
        value: "",
        style: {},
        setAttribute: vi.fn(),
        focus: vi.fn(),
        select: vi.fn(),
        setSelectionRange: vi.fn(),
        remove: vi.fn(),
      }),
      body: { appendChild: vi.fn() },
      execCommand: () => false,
    });

    await expect(copyToClipboard("内容")).rejects.toThrow("无法写入剪贴板");
  });

  it("ignores empty text", async () => {
    const invoke = vi.fn(async () => true);
    stubDesktop(invoke);
    await copyToClipboard("");
    expect(invoke).not.toHaveBeenCalled();
  });
});

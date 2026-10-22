import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  createBodyScrollLock,
  nextFocusIndex,
  OVERLAY_FOCUSABLE_SELECTOR,
  type BodyScrollLockTarget,
} from "./Overlay";

const source = readFileSync(fileURLToPath(new URL("./Overlay.tsx", import.meta.url)), "utf8");
const css = readFileSync(fileURLToPath(new URL("../overlay.css", import.meta.url)), "utf8");

function declarations(selector: string): string {
  const marker = `${selector} {`;
  const start = css.indexOf(marker);
  expect(start, `missing CSS rule for ${selector}`).toBeGreaterThanOrEqual(0);
  const end = css.indexOf("}", start);
  return css.slice(start + marker.length, end);
}

function fakeBody(clientWidth: number, overflow = "auto", paddingRight = ""): BodyScrollLockTarget {
  return { style: { overflow, paddingRight }, clientWidth };
}

describe("Overlay body scroll lock", () => {
  it("locks and restores body scroll for a single dialog", () => {
    const body = fakeBody(1000);
    const lock = createBodyScrollLock({ getViewportWidth: () => 1000 });
    const release = lock.acquire(body);
    expect(lock.active).toBe(true);
    expect(body.style.overflow).toBe("hidden");
    expect(body.style.paddingRight).toBe("");
    release();
    expect(lock.active).toBe(false);
    expect(body.style.overflow).toBe("auto");
  });

  it("compensates the scrollbar width without clobbering existing padding", () => {
    const body = fakeBody(1000, "", "4px");
    const lock = createBodyScrollLock({ getViewportWidth: () => 1010 });
    const release = lock.acquire(body);
    expect(body.style.overflow).toBe("hidden");
    expect(body.style.paddingRight).toBe("14px");
    release();
    expect(body.style.paddingRight).toBe("4px");
  });

  it("reference-counts nested dialogs and only restores on the last release", () => {
    const body = fakeBody(1000);
    const lock = createBodyScrollLock({ getViewportWidth: () => 1000 });
    const releaseFirst = lock.acquire(body);
    const releaseSecond = lock.acquire(body);
    releaseFirst();
    expect(lock.active).toBe(true);
    expect(body.style.overflow).toBe("hidden");
    releaseSecond();
    expect(lock.active).toBe(false);
    expect(body.style.overflow).toBe("auto");
  });

  it("keeps release idempotent", () => {
    const body = fakeBody(1000);
    const lock = createBodyScrollLock({ getViewportWidth: () => 1000 });
    const release = lock.acquire(body);
    release();
    release();
    expect(lock.active).toBe(false);
    expect(body.style.overflow).toBe("auto");
  });
});

describe("Overlay focus trap", () => {
  it("wraps focus forward and backwards", () => {
    expect(nextFocusIndex(3, 0, false)).toBe(1);
    expect(nextFocusIndex(3, 2, false)).toBe(0);
    expect(nextFocusIndex(3, 2, true)).toBe(1);
    expect(nextFocusIndex(3, 0, true)).toBe(2);
    expect(nextFocusIndex(3, -1, false)).toBe(0);
    expect(nextFocusIndex(3, -1, true)).toBe(2);
    expect(nextFocusIndex(0, 0, false)).toBe(-1);
  });

  it("includes standard dialog controls in the focusable query", () => {
    for (const token of ["button:not([disabled])", "input:not([disabled])", "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])"]) {
      expect(OVERLAY_FOCUSABLE_SELECTOR).toContain(token);
    }
  });
});

describe("Overlay implementation contract", () => {
  it("renders through a portal on document.body with dialog semantics", () => {
    expect(source).toContain("createPortal(");
    expect(source).toContain("document.body");
    expect(source).toContain('role="dialog"');
    expect(source).toContain('aria-modal="true"');
    expect(source).toContain("aria-labelledby={titleId}");
  });

  it("closes on Escape and keeps focus inside the panel", () => {
    expect(source).toContain('event.key === "Escape"');
    expect(source).toContain("overlayStack");
    expect(source).toContain("nextFocusIndex(");
    expect(source).toContain('event.key !== "Tab"');
    expect(source).toContain("previousFocus.current");
  });

  it("locks both body scroll and the page-frame scroll container", () => {
    expect(source).toContain('bodyScrollLock.acquire(document.body)');
    expect(source).toContain('classList.add("overlay-open")');
    expect(source).toContain('classList.remove("overlay-open")');
    expect(declarations(".overlay-root")).toMatch(/position:\s*fixed/);
    expect(declarations(".overlay-root")).toMatch(/inset:\s*0/);
    expect(css).toContain("html.overlay-open .page-frame");
    expect(css).toMatch(/html\.overlay-open \.page-frame \{\s*overflow:\s*hidden/);
  });

  it("sizes the panel with dvh so 100/110/125% zoom and 1280x820 do not clip", () => {
    const panel = declarations(".overlay-panel");
    expect(panel).toMatch(/max-height:\s*calc\(100dvh\s*-\s*var\(--overlay-gap\)\s*-\s*var\(--overlay-gap\)\)/);
    expect(panel).toMatch(/min-height:\s*0/);
    expect(panel).toMatch(/overflow:\s*hidden/);
    const body = declarations(".overlay-body");
    expect(body).toMatch(/overflow-y:\s*auto/);
    expect(body).toMatch(/overscroll-behavior:\s*contain/);
    expect(css).toContain("@media (max-height: 720px)");
  });

  it("uses shared type/control tokens and has a dark backdrop", () => {
    expect(css).not.toMatch(/font-size:\s*[0-9.]+px/);
    expect(css).toContain("var(--fs-caption)");
    expect(css).toContain("var(--control-h)");
    expect(css).toContain(".dark .overlay-backdrop");
    expect(declarations(".overlay-footer")).toMatch(/flex-wrap:\s*wrap/);
  });
});

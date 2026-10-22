import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * P2 regression guard: schedules dialogs must go through the portal Overlay and
 * the shell must keep a single page scroll container. Text-level checks match the
 * existing repo testing style (no jsdom/testing-library dependency is installed).
 */

const read = (path: string) => readFileSync(fileURLToPath(new URL(path, import.meta.url)), "utf8");

const editor = read("./ScheduleEditorModal.tsx");
const history = read("./ExecutionHistoryModal.tsx");
const pending = read("./PendingSyncModal.tsx");
const confirm = read("./ConfirmDialog.tsx");
const page = read("../SchedulesPage.tsx");
const navCss = read("../../nav.css");
const indexCss = read("../../index.css");
const redesignCss = read("../../redesign.css");
const overlayCss = read("../../overlay.css");

function declarations(css: string, selector: string): string {
  const marker = `\n${selector} {`;
  const start = css.indexOf(marker);
  expect(start, `missing CSS rule for ${selector}`).toBeGreaterThanOrEqual(0);
  const end = css.indexOf("}", start);
  return css.slice(start + marker.length, end);
}

describe("schedules dialogs use the shared Overlay", () => {
  it("imports and renders Overlay in all three dialogs", () => {
    for (const source of [editor, history, pending]) {
      expect(source).toContain('from "../../components/Overlay"');
      expect(source).toContain("<Overlay");
      expect(source).toContain("open");
      expect(source).toContain("onClose={onClose}");
      expect(source).toContain("footer={(");
    }
  });

  it("removes page-level fixed modal markup from schedules", () => {
    for (const source of [editor, history, pending]) {
      expect(source).not.toContain("modal-backdrop");
      expect(source).not.toContain("schedule-modal-backdrop");
    }
    expect(editor).not.toContain('<div className="schedule-modal">');
    expect(history).not.toContain('role="dialog"'); // Overlay owns dialog semantics now
  });

  it("replaces native window.confirm with a portal confirmation dialog", () => {
    expect(page).not.toContain("window.confirm");
    expect(page).toContain("<ConfirmDialog");
    expect(page).toContain("setConfirmRequest");
    expect(confirm).toContain('from "../../components/Overlay"');
    expect(confirm).toContain("<Overlay");
    expect(confirm).toContain('tone === "danger" ? "danger" : "primary"');
  });

  it("keeps busy dialogs open while writes are in flight", () => {
    expect(editor).toContain("closeDisabled={saving}");
    expect(pending).toContain("closeDisabled={busy}");
    expect(history).toContain("onClose={onClose}");
  });

  it("keeps the guarded external-write copy required by the layout test", () => {
    expect(editor).toContain('label="自动同步腾讯文档"');
    expect(editor).toContain("disabled={!autoSyncAvailable}");
    expect(editor).toContain("无需手动确认；任务完成后直接写入已选模板");
    expect(editor).toContain("这是外部写入操作");
    expect(page).toContain("<ScheduleEditorModal");
    expect(page).toContain("<ExecutionHistoryModal");
    expect(page).toContain("<PendingSyncModal");
  });
});

describe("single scroll shell", () => {
  it("keeps .page-frame as the only page scroller", () => {
    const frame = declarations(navCss, ".page-frame");
    expect(frame).toMatch(/overflow:\s*auto/);
    expect(frame).toMatch(/min-height:\s*0/);
    expect(frame).toMatch(/height:\s*100%/);
    expect(frame).toMatch(/scrollbar-gutter:\s*stable/);
    expect(frame).toContain("overscroll-behavior: contain");

    const content = declarations(indexCss, ".page-content");
    expect(content).not.toMatch(/overflow(-y|-x)?\s*:/);
    expect(content).toContain("min-height: 100%");
  });

  it("keeps legacy .main-panel non-scrolling and subtracts the titlebar once", () => {
    expect(declarations(indexCss, ".main-panel")).toContain("overflow: hidden");
    for (const css of [indexCss, navCss, redesignCss]) {
      const appBody = declarations(css, ".app-body");
      expect(appBody).toContain("var(--control-h");
      expect(appBody).toMatch(/min-height:\s*0/);
    }
  });

  it("does not leave a transform on the page wrapper that clips fixed dialogs", () => {
    expect(redesignCss).toContain(".page-frame > *");
    expect(redesignCss).not.toContain("animation: page-enter .22s ease both");
    expect(redesignCss).not.toContain("transform: translateY(5px)");
    expect(overlayCss).toContain("html.overlay-open .page-frame");
  });

  it("locks the page scroller for the portal overlay, not only body", () => {
    expect(overlayCss).toMatch(/html\.overlay-open,\s*html\.overlay-open body\s*\{\s*overflow:\s*hidden/);
    expect(overlayCss).toMatch(/html\.overlay-open \.page-frame\s*\{\s*overflow:\s*hidden/);
  });
});

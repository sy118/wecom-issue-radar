import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const css = readFileSync(
  fileURLToPath(new URL("../index.css", import.meta.url)),
  "utf8",
);
const source = readFileSync(
  fileURLToPath(new URL("./SchedulesPage.tsx", import.meta.url)),
  "utf8",
);

function declarations(selector: string): string {
  const marker = `${selector} {`;
  const start = css.indexOf(marker);
  expect(start, `missing CSS rule for ${selector}`).toBeGreaterThanOrEqual(0);
  const end = css.indexOf("}", start);
  expect(end, `unterminated CSS rule for ${selector}`).toBeGreaterThan(start);
  return css.slice(start + marker.length, end);
}

describe("schedule editor modal layout", () => {
  it("keeps the long form inside the viewport with an independently scrollable body", () => {
    const backdrop = declarations(".modal-backdrop.schedule-modal-backdrop");
    expect(backdrop).toMatch(/align-items:\s*stretch/);
    expect(backdrop).toMatch(/justify-items:\s*end/);
    expect(backdrop).toMatch(/padding:\s*0/);

    const modal = declarations(".schedule-modal");
    expect(modal).toMatch(/display:\s*flex/);
    expect(modal).toMatch(/flex-direction:\s*column/);
    expect(modal).toMatch(/max-height:\s*calc\(100vh\s*-\s*\d+px\)/);
    expect(modal).toMatch(/overflow:\s*hidden/);

    const body = declarations(".schedule-modal-body");
    expect(body).toMatch(/min-height:\s*0/);
    expect(body).toMatch(/overflow-y:\s*auto/);
    expect(body).toMatch(/overscroll-behavior:\s*contain/);

    const footer = declarations(".schedule-modal-footer");
    expect(footer).toMatch(/flex:\s*0\s+0\s+auto/);
  });

  it("keeps execution history scrollable with pagination outside the scroll body", () => {
    const modal = declarations(".schedule-history-modal");
    expect(modal).toMatch(/width:\s*min\(/);

    const body = declarations(".schedule-history-body");
    expect(body).toMatch(/display:\s*block/);

    const footer = declarations(".schedule-history-footer");
    expect(footer).toMatch(/align-items:\s*center/);

    const sharedBody = declarations(".schedule-modal-body");
    expect(sharedBody).toMatch(/overflow-y:\s*auto/);
    const sharedFooter = declarations(".schedule-modal-footer");
    expect(sharedFooter).toMatch(/flex:\s*0\s+0\s+auto/);
  });

  it("shows automatic sync as a guarded external-write option", () => {
    expect(source).toContain('label="自动同步腾讯文档"');
    expect(source).toContain("disabled={!autoSyncAvailable}");
    expect(source).toContain("无需手动确认；任务完成后直接写入已选模板");
    expect(source).toContain("这是外部写入操作");

    const risk = declarations(".schedule-auto-sync-risk");
    expect(risk).toMatch(/border:/);
    expect(risk).toMatch(/warning/);
  });
});

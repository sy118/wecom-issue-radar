import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const source = readFileSync(
  fileURLToPath(new URL("./PromptsPage.tsx", import.meta.url)),
  "utf8",
);
const css = readFileSync(
  fileURLToPath(new URL("../index.css", import.meta.url)),
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

describe("prompt editor layout", () => {
  it("keeps the save action visible in a sticky bar while the long editor scrolls", () => {
    expect(source).toMatch(/className="prompt-save-bar"[\s\S]*?onClick=\{\(\) => void save\(\)\}/);

    const saveBar = declarations(".prompt-save-bar");
    expect(saveBar).toMatch(/position:\s*sticky/);
    expect(saveBar).toMatch(/top:\s*\d+px/);
    expect(saveBar).toMatch(/z-index:\s*\d+/);
  });
});

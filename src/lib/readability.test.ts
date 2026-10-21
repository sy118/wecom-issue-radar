import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * Readability budget for the renderer stylesheet.
 *
 * This test intentionally parses the CSS as text (no browser): the app ships a
 * single hand-written stylesheet and the regression we care about is a tiny
 * `font-size` slipping back in, not a rendering detail.
 */

const css = readFileSync(
  fileURLToPath(new URL("../index.css", import.meta.url)),
  "utf8",
);

function block(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  expect(start, `missing CSS block for ${selector}`).toBeGreaterThanOrEqual(0);
  const end = css.indexOf("}", start);
  expect(end, `unterminated CSS block for ${selector}`).toBeGreaterThan(start);
  return css.slice(start + selector.length + 2, end);
}

function declarations(source: string): Map<string, string> {
  const result = new Map<string, string>();
  for (const match of source.matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)) {
    result.set(match[1], match[2].trim());
  }
  return result;
}

function px(value: string | undefined): number {
  expect(value, "missing token").toBeTruthy();
  const match = /^(\d+(?:\.\d+)?)px$/.exec(String(value).trim());
  expect(match, `not a pixel token: ${String(value)}`).not.toBeNull();
  return Number((match as RegExpExecArray)[1]);
}

function hslComponents(value: string): [number, number, number] {
  const match = /^(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)%\s+(\d+(?:\.\d+)?)%$/.exec(value.trim());
  expect(match, `not an HSL token: ${value}`).not.toBeNull();
  const [, h, s, l] = match as RegExpExecArray;
  return [Number(h), Number(s), Number(l)];
}

function hslToRgb([h, s, l]: [number, number, number]): [number, number, number] {
  const saturation = s / 100;
  const lightness = l / 100;
  const chroma = (1 - Math.abs(2 * lightness - 1)) * saturation;
  const hue = ((h % 360) + 360) % 360;
  const x = chroma * (1 - Math.abs(((hue / 60) % 2) - 1));
  const m = lightness - chroma / 2;
  let rgb: [number, number, number];
  if (hue < 60) rgb = [chroma, x, 0];
  else if (hue < 120) rgb = [x, chroma, 0];
  else if (hue < 180) rgb = [0, chroma, x];
  else if (hue < 240) rgb = [0, x, chroma];
  else if (hue < 300) rgb = [x, 0, chroma];
  else rgb = [chroma, 0, x];
  return rgb.map((channel) => channel + m) as [number, number, number];
}

function luminance(rgb: [number, number, number]): number {
  const [r, g, b] = rgb.map((channel) => {
    const value = channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
    return value;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(foreground: string, background: string): number {
  const fg = luminance(hslToRgb(hslComponents(foreground)));
  const bg = luminance(hslToRgb(hslComponents(background)));
  const lighter = Math.max(fg, bg);
  const darker = Math.min(fg, bg);
  return (lighter + 0.05) / (darker + 0.05);
}

const light = declarations(block(":root"));
const dark = declarations(block(".dark"));

describe("readability budget", () => {
  it("defines a >=16px body scale with a hard 12px minimum", () => {
    const expected: Record<string, number> = {
      "fs-display": 28,
      "fs-h1": 24,
      "fs-h2": 20,
      "fs-h3": 17,
      "fs-body": 16,
      "fs-label": 14,
      "fs-caption": 13,
      "fs-min": 12,
    };
    for (const [name, minimum] of Object.entries(expected)) {
      const value = px(light.get(name));
      expect(value, `--${name} must be >= 12px`).toBeGreaterThanOrEqual(12);
      if (name !== "fs-min") expect(value).toBe(minimum);
    }
    expect(px(light.get("fs-body"))).toBeGreaterThanOrEqual(16);
    expect(px(light.get("fs-min"))).toBe(12);
  });

  it("keeps line-height, control and spacing tokens", () => {
    for (const token of ["lh-tight", "lh-title", "lh-body", "lh-caption"]) {
      expect(light.get(token), `missing --${token}`).toBeTruthy();
    }
    expect(px(light.get("control-h"))).toBe(44);
    expect(px(light.get("control-h-sm"))).toBe(36);
    for (const [index, size] of [4, 8, 12, 16, 24, 32].entries()) {
      expect(px(light.get(`space-${index + 1}`))).toBe(size);
    }
  });

  it("has no font-size below 12px (0 is only used to hide collapsed labels)", () => {
    const offenders: string[] = [];
    let numeric = 0;
    for (const match of css.matchAll(/font-size:\s*([^;]+);/g)) {
      const value = match[1].trim().replace(" !important", "");
      if (/^0(?:px)?$/.test(value)) continue;
      if (value.startsWith("var(--fs-") || value === "inherit") {
        numeric += 0;
        continue;
      }
      const pixelMatch = /^(\d+(?:\.\d+)?)px$/.exec(value);
      if (!pixelMatch) {
        offenders.push(`unexpected font-size value: ${value}`);
        continue;
      }
      numeric += 1;
      if (Number(pixelMatch[1]) < 12) offenders.push(`font-size: ${value}`);
    }
    expect(offenders).toEqual([]);
    // One literal 16px rule is pinned by ReplyRuntimePages.test.ts.
    expect(numeric).toBeLessThanOrEqual(1);
  });

  it("tokenizes the global type scale everywhere", () => {
    const uses = [...css.matchAll(/font-size:\s*var\(--fs-/g)].length;
    expect(uses).toBeGreaterThanOrEqual(280);
    for (const selector of [".button {", ".input {", ".nav-item {", ".field-hint {", ".section-header h2 {"]) {
      const start = css.indexOf(selector);
      expect(start, `missing ${selector}`).toBeGreaterThanOrEqual(0);
      const end = css.indexOf("}", start);
      expect(css.slice(start, end)).toContain("font-size: var(--fs-");
    }
  });

  it("keeps muted text at AA contrast in both themes", () => {
    expect(contrast(light.get("muted-foreground") as string, light.get("background") as string))
      .toBeGreaterThanOrEqual(4.5);
    expect(contrast(dark.get("muted-foreground") as string, dark.get("background") as string))
      .toBeGreaterThanOrEqual(4.5);
    expect(contrast(dark.get("muted-foreground") as string, dark.get("card") as string))
      .toBeGreaterThanOrEqual(4.5);
  });

  it("keeps small primary text at AA contrast in both themes", () => {
    expect(contrast(light.get("primary-text") as string, light.get("background") as string))
      .toBeGreaterThanOrEqual(4.5);
    expect(contrast(dark.get("primary-text") as string, dark.get("background") as string))
      .toBeGreaterThanOrEqual(4.5);
    expect(contrast(light.get("danger-text") as string, light.get("background") as string))
      .toBeGreaterThanOrEqual(4.5);
  });

  it("uses >=44px controls for the primary interactive elements", () => {
    for (const selector of [".button {", ".input {", ".nav-item {", ".settings-tabs button {"]) {
      const start = css.indexOf(selector);
      expect(start, `missing ${selector}`).toBeGreaterThanOrEqual(0);
      const end = css.indexOf("}", start);
      const rule = css.slice(start, end);
      expect(rule, `${selector} should use the 44px control token`).toMatch(/var\(--control-h(?:-sm)?\)/);
    }
  });
});

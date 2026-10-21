import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * The daily assistant CSS is outside index.css, so the readability budget is
 * enforced here: every type size must come from the shared token scale and no
 * declaration may drop below the 12px minimum.
 */
const css = readFileSync(fileURLToPath(new URL("./daily.css", import.meta.url)), "utf8");

describe("daily assistant styles", () => {
  it("uses only shared font-size tokens", () => {
    const values = [...css.matchAll(/font-size:\s*([^;]+);/g)].map((match) => match[1].trim());
    expect(values.length).toBeGreaterThan(0);
    for (const value of values) {
      expect(value, `unexpected font-size: ${value}`).toMatch(
        /^var\(--fs-(display|h1|h2|h3|body|label|caption|min)\)$/,
      );
    }
  });

  it("never declares a raw pixel size below 12px", () => {
    const offenders = [...css.matchAll(/font-size:\s*([0-9.]+)px/g)]
      .map((match) => Number(match[1]))
      .filter((size) => size < 12);
    expect(offenders).toEqual([]);
  });

  it("reuses the shared control and spacing tokens", () => {
    expect(css).toContain("var(--control-h)");
    expect(css).toContain("var(--space-3)");
    expect(css).toContain("var(--lh-body)");
  });
});

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const source = readFileSync(
  fileURLToPath(new URL("./PromptsPage.tsx", import.meta.url)),
  "utf8",
);

describe("prompt field editor identity", () => {
  it("does not use the editable field key as a React list key", () => {
    const mutableFieldKeyLines = source
      .split(/\r?\n/)
      .filter((line) => line.includes("key=") && /field\.key/.test(line));

    expect(mutableFieldKeyLines).toEqual([]);
  });
});

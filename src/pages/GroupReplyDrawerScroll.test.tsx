import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const pagePath = fileURLToPath(
  new URL("./groupreply/ListenerEditorDrawer.tsx", import.meta.url),
);
const pageSource = readFileSync(pagePath, "utf8");
const css = readFileSync(
  fileURLToPath(new URL("../overlay.css", import.meta.url)),
  "utf8",
);
const sourceFile = ts.createSourceFile(
  pagePath,
  pageSource,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

function classNameOf(node: ts.JsxElement): string {
  const attribute = node.openingElement.attributes.properties.find((candidate) => (
    ts.isJsxAttribute(candidate) && candidate.name.getText(sourceFile) === "className"
  ));
  return attribute && ts.isJsxAttribute(attribute) && attribute.initializer
    ? attribute.initializer.getText(sourceFile).replace(/^['"]|['"]$/g, "")
    : "";
}

function findElementByClass(className: string): ts.JsxElement | undefined {
  let match: ts.JsxElement | undefined;
  const visit = (node: ts.Node) => {
    if (!match && ts.isJsxElement(node) && classNameOf(node).split(/\s+/).includes(className)) {
      match = node;
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return match;
}

function isDescendantOf(node: ts.Node, ancestor: ts.Node): boolean {
  let current: ts.Node | undefined = node.parent;
  while (current) {
    if (current === ancestor) return true;
    current = current.parent;
  }
  return false;
}

function declarations(selector: string): string {
  const marker = `\n${selector} {`;
  const start = css.indexOf(marker);
  expect(start, `missing CSS rule for ${selector}`).toBeGreaterThanOrEqual(0);
  const end = css.indexOf("}", start);
  expect(end, `unterminated CSS rule for ${selector}`).toBeGreaterThan(start);
  return css.slice(start + marker.length, end);
}

describe("group reply drawer scrolling", () => {
  it("keeps the native fieldset inside the shared portal overlay body", () => {
    const overlayStart = pageSource.indexOf("<Overlay");
    const overlayEnd = pageSource.indexOf("</Overlay>");
    expect(overlayStart, "missing shared Overlay wrapper").toBeGreaterThanOrEqual(0);
    expect(overlayEnd).toBeGreaterThan(overlayStart);
    expect(pageSource).toContain("closeDisabled={Boolean(busy)}");

    const fields = findElementByClass("runtime-drawer-fields");
    expect(fields, "missing busy-state fieldset inside the overlay").toBeDefined();
    expect(fields?.openingElement.tagName.getText(sourceFile)).toBe("fieldset");
    expect(fields?.openingElement.attributes.getText(sourceFile))
      .toContain("disabled={Boolean(busy)}");

    const steps = findElementByClass("runtime-drawer-steps");
    expect(steps, "missing four-step editor navigation").toBeDefined();
    expect(steps?.openingElement.tagName.getText(sourceFile)).toBe("nav");
    expect(steps?.getStart(sourceFile) ?? 0).toBeGreaterThan(overlayStart);
    expect(steps?.getEnd() ?? 0).toBeLessThan(overlayEnd);

    const panel = declarations(".overlay-panel");
    expect(panel).toMatch(/display:\s*flex/);
    expect(panel).toMatch(/flex-direction:\s*column/);
    expect(panel).toMatch(/max-height:\s*calc\(100dvh/);
    expect(panel).toMatch(/overflow:\s*hidden/);

    const scrollBody = declarations(".overlay-body");
    expect(scrollBody).toMatch(/flex:\s*1\s+1\s+auto/);
    expect(scrollBody).toMatch(/min-height:\s*0/);
    expect(scrollBody).toMatch(/overflow-y:\s*auto/);
  });
});

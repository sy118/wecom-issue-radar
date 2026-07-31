import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const pagePath = fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url));
const pageSource = readFileSync(pagePath, "utf8");
const css = readFileSync(
  fileURLToPath(new URL("../index.css", import.meta.url)),
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
  it("keeps the native fieldset inside the independently scrollable drawer body", () => {
    const body = findElementByClass("runtime-drawer-body");
    expect(body, "missing group reply drawer body").toBeDefined();
    expect(body?.openingElement.tagName.getText(sourceFile)).toBe("div");
    expect(body && ts.isJsxElement(body.parent) ? classNameOf(body.parent) : "")
      .toContain("reply-drawer");

    const fields = findElementByClass("runtime-drawer-fields");
    expect(fields, "missing busy-state fieldset inside the scroll body").toBeDefined();
    expect(fields?.openingElement.tagName.getText(sourceFile)).toBe("fieldset");
    expect(body && fields ? isDescendantOf(fields, body) : false).toBe(true);
    expect(fields?.openingElement.attributes.getText(sourceFile))
      .toContain("disabled={Boolean(busy)}");

    const toolListElement = findElementByClass("tool-grant-list");
    expect(toolListElement, "missing MCP tool grant list").toBeDefined();
    expect(fields && toolListElement ? isDescendantOf(toolListElement, fields) : false)
      .toBe(true);

    const drawer = declarations(".schedule-modal");
    expect(drawer).toMatch(/display:\s*flex/);
    expect(drawer).toMatch(/flex-direction:\s*column/);
    expect(drawer).toMatch(/max-height:\s*calc\(100(?:d)?vh\s*-\s*\d+px\)/);
    expect(drawer).toMatch(/overflow:\s*hidden/);

    const scrollBody = declarations(".schedule-modal-body");
    expect(scrollBody).toMatch(/flex:\s*1\s+1\s+auto/);
    expect(scrollBody).toMatch(/min-height:\s*0/);
    expect(scrollBody).toMatch(/overflow-y:\s*auto/);

    const grantList = declarations(".tool-grant-list");
    expect(grantList).toMatch(/max-height:\s*\d+px/);
    expect(grantList).toMatch(/overflow-y:\s*auto/);
    expect(grantList).toMatch(/grid-auto-rows:\s*max-content/);
    expect(grantList).toMatch(/align-content:\s*start/);
  });
});

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const pagePath = fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url));
const pageSource = readFileSync(pagePath, "utf8");
const editorPath = fileURLToPath(
  new URL("./groupreply/ListenerEditorDrawer.tsx", import.meta.url),
);
const editorSource = readFileSync(editorPath, "utf8");
const sourceFile = ts.createSourceFile(
  pagePath,
  pageSource,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);
const editorSourceFile = ts.createSourceFile(
  editorPath,
  editorSource,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

function findNode<T extends ts.Node>(predicate: (node: ts.Node) => node is T): T {
  let match: T | undefined;
  const visit = (node: ts.Node) => {
    if (!match && predicate(node)) match = node;
    if (!match) ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  if (!match) throw new Error("GroupReplyPage webhook flow node was not found");
  return match;
}

function findEditorNode<T extends ts.Node>(predicate: (node: ts.Node) => node is T): T {
  let match: T | undefined;
  const visit = (node: ts.Node) => {
    if (!match && predicate(node)) match = node;
    if (!match) ts.forEachChild(node, visit);
  };
  visit(editorSourceFile);
  if (!match) throw new Error("ListenerEditorDrawer webhook flow node was not found");
  return match;
}

function sendTestDisabledExpression(): string {
  const button = findEditorNode((node): node is ts.JsxElement => (
    ts.isJsxElement(node)
    && node.openingElement.tagName.getText(editorSourceFile) === "Button"
    && node.getText(editorSourceFile).includes("发送测试")
  ));
  const disabled = button.openingElement.attributes.properties.find((attribute) => (
    ts.isJsxAttribute(attribute) && attribute.name.getText(editorSourceFile) === "disabled"
  ));
  if (!disabled || !ts.isJsxAttribute(disabled) || !disabled.initializer
    || !ts.isJsxExpression(disabled.initializer) || !disabled.initializer.expression) {
    return "";
  }
  return disabled.initializer.expression.getText(editorSourceFile);
}

function testWebhookHandlerSource(): string {
  const declaration = findNode((node): node is ts.VariableDeclaration => (
    ts.isVariableDeclaration(node) && node.name.getText(sourceFile) === "testWebhook"
  ));
  return declaration.initializer?.getText(sourceFile) ?? "";
}

function persistListenerDraftSource(): string {
  const declaration = findNode((node): node is ts.VariableDeclaration => (
    ts.isVariableDeclaration(node) && node.name.getText(sourceFile) === "persistListenerDraft"
  ));
  return declaration.initializer?.getText(sourceFile) ?? "";
}

describe("new listener webhook verification flow", () => {
  it("keeps the visible webhook test reachable before the listener or replacement URL is persisted", () => {
    const disabledExpression = sendTestDisabledExpression();
    const handler = testWebhookHandlerSource();
    const blockers = [
      /!draft\.id\b/.test(disabledExpression)
        ? "发送测试按钮在新监听器尚无 id 时被置灰"
        : "",
      /webhookMode\s*!==\s*["']keep["']/.test(disabledExpression)
        ? "发送测试按钮在新填或替换 webhook 时被置灰"
        : "",
      /if\s*\(\s*!draft\.id\s*\)\s*return/.test(handler)
        ? "点击处理也拒绝尚未持久化的新监听器"
        : "",
    ].filter(Boolean);

    expect(blockers).toEqual([]);
    expect(handler).toContain('deliveryMode: "review" as const');
    expect(handler).toContain("persistListenerDraft(safeDraft)");
    expect(handler.indexOf("persistListenerDraft(safeDraft)"))
      .toBeLessThan(handler.indexOf('kind: "listener.test_webhook"'));
    expect(handler).toContain("setDraft(persistedDraft)");
    expect(editorSource).toContain("aria-busy={Boolean(busy)}");
    expect(editorSource).toContain('<div className="schedule-modal-body runtime-drawer-body">');
    expect(editorSource).toContain('<fieldset className="runtime-drawer-fields" disabled={Boolean(busy)}>');
    expect(editorSource).toContain('<Button onClick={() => onSave()} disabled={Boolean(busy)}>');
  });

  it("uses the live revision for a new listener and reports pre-save failures before testing webhook", () => {
    const persistHandler = persistListenerDraftSource();
    const testHandler = testWebhookHandlerSource();

    expect(persistHandler).toContain("executeListenerSave<unknown>");
    expect(persistHandler).toContain('createQuery({ kind: "listener.list" })');
    expect(testHandler.match(/catch\s*\(/g)).toHaveLength(2);
    expect(testHandler).toContain('toast.error("保存失败"');
    expect(testHandler).not.toContain("savedBeforeTest");
    expect(testHandler.indexOf('toast.error("保存失败"'))
      .toBeLessThan(testHandler.indexOf('kind: "listener.test_webhook"'));
  });
});

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), "utf8");
}

/**
 * Regression guard for the v4.6.9 advanced-settings shell. The advanced draft
 * used to be unsaveable because nav.css hid the only page-title-row holding the
 * save button, so these source assertions lock the shell contract instead of a
 * brittle render snapshot (this repo has no DOM test environment).
 */
describe("advanced settings shell", () => {
  it("does not hide the advanced page header row in embedded mode", () => {
    const nav = source("../../nav.css");
    expect(nav).not.toContain(".settings-advanced-embed > .settings-page > .page-title-row");
    expect(nav).not.toMatch(/settings-advanced-embed[\s\S]{0,120}display:\s*none/);
  });

  it("renders an embedded toolbar, save state and sticky save dock", () => {
    const settings = source("../SettingsPage.tsx");
    for (const marker of [
      "embedded?: boolean",
      "onDirtyChange?: (dirty: boolean) => void",
      "settings-advanced-shell",
      "settings-advanced-toolbar",
      "settings-advanced-tabs",
      "settings-save-state",
      "settings-save-dock",
      "高级设置",
      "有未保存修改",
      "已保存",
      "保存设置",
      "useUnsavedChanges(\"settings-advanced\", hasUnsavedChanges)",
    ]) {
      expect(settings).toContain(marker);
    }
    // Both save entries share the same disabled guard and handler.
    expect(settings).toContain("disabled={!hasUnsavedChanges || saving}");
    expect(settings.match(/onClick=\{\(\) => void save\(\)\}/g)?.length).toBe(1);
    expect(settings).toContain("const saveButton = (className?: string) =>");
  });

  it("keeps the low-frequency transfer block outside the active tab", () => {
    const settings = source("../SettingsPage.tsx");
    const migration = settings.indexOf("settings-advanced-migration");
    const environmentTab = settings.indexOf('tab === "environment"');
    expect(migration).toBeGreaterThan(environmentTab);
    expect(settings).toContain("配置备份与迁移");
    expect(settings).toContain("备份范围");
  });

  it("passes the embedded dirty flag and confirms mode switches in SettingsHome", () => {
    const home = source("../SettingsHome.tsx");
    for (const marker of [
      "advancedDirty",
      "requestMode",
      "当前模式还有未保存的修改",
      "embedded",
      "onDirtyChange={setAdvancedDirty}",
      'mode === "advanced" ? "设置" : "常用设置"',
    ]) {
      expect(home).toContain(marker);
    }
    // Common draft is discarded only after the user confirms leaving it.
    expect(home).toContain("if (mode === \"common\" && dirty) setDraft(config);");
  });

  it("styles the shell with shared tokens and supports narrow windows", () => {
    const css = source("../../redesign.css");
    for (const marker of [
      ".settings-advanced-shell",
      ".settings-advanced-toolbar",
      ".settings-save-dock",
      ".settings-advanced-migration",
      ".settings-advanced-tabs",
      "grid-template-columns: repeat(2, minmax(0, 1fr))",
      "@media (max-width: 899px)",
    ]) {
      expect(css).toContain(marker);
    }
    expect(css).toContain("var(--elev-1)");
    expect(css).toContain("var(--radius-lg)");
  });
});

import { describe, expect, it } from "vitest";

import {
  entryLooksLikeLegacy,
  extractUninstallerPath,
  parseRegistryQueryOutput,
  selectLegacyInstall,
} from "../migrate";

const REGISTRY_FIXTURE = `
HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{11111111-1111-1111-1111-111111111111}
    DisplayName    REG_SZ    企微问题雷达
    DisplayVersion    REG_SZ    3.5.3
    InstallLocation    REG_SZ    C:\\Users\\admin\\AppData\\Local\\企微问题雷达
    DisplayIcon    REG_SZ    "C:\\Users\\admin\\AppData\\Local\\企微问题雷达\\wecom-issue-radar.exe",0
    UninstallString    REG_SZ    "C:\\Users\\admin\\AppData\\Local\\企微问题雷达\\uninstall.exe"

HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{22222222-2222-2222-2222-222222222222}
    DisplayName    REG_SZ    Microsoft Edge
    DisplayVersion    REG_SZ    130.0.0.0
    InstallLocation    REG_SZ    C:\\Program Files (x86)\\Microsoft\\Edge
    UninstallString    REG_SZ    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\setup.exe" --uninstall
`;

describe("legacy uninstall registry parsing", () => {
  it("parses blocks, types and quoted values", () => {
    const entries = parseRegistryQueryOutput(REGISTRY_FIXTURE);
    expect(entries).toHaveLength(2);
    expect(entries[0]).toMatchObject({
      displayName: "企微问题雷达",
      displayVersion: "3.5.3",
      installLocation: "C:\\Users\\admin\\AppData\\Local\\企微问题雷达",
    });
    expect(entries[1]?.displayName).toBe("Microsoft Edge");
  });

  it("ignores unrelated entries and selects the legacy install", () => {
    const entries = parseRegistryQueryOutput(REGISTRY_FIXTURE);
    expect(entryLooksLikeLegacy(entries[0]!)).toBe(true);
    expect(entryLooksLikeLegacy(entries[1]!)).toBe(false);
    expect(selectLegacyInstall(entries)).toEqual({
      installed: true,
      version: "3.5.3",
      uninstallerPath:
        "C:\\Users\\admin\\AppData\\Local\\企微问题雷达\\uninstall.exe",
      installDir: "C:\\Users\\admin\\AppData\\Local\\企微问题雷达",
    });
  });

  it("returns installed=false when nothing matches", () => {
    expect(
      selectLegacyInstall(
        parseRegistryQueryOutput(
          "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\x\n    DisplayName    REG_SZ    Notepad\n",
        ),
      ),
    ).toEqual({ installed: false });
  });

  it("prefers the newest version and falls back to the DisplayIcon directory", () => {
    const older = {
      key: "HKEY_A",
      displayName: "WeCom Issue Radar",
      displayVersion: "3.4.0",
      uninstallString: '"C:\\Apps\\radar-old\\uninstall.exe" /S',
      installLocation: "C:\\Apps\\radar-old",
    };
    const newer = {
      key: "HKEY_B",
      displayName: "企微问题雷达",
      displayVersion: "3.5.3",
      uninstallString:
        '"C:\\Apps\\radar-new\\uninstall.exe" /S --keep-data',
      displayIcon: '"C:\\Apps\\radar-new\\app.exe",0',
    };
    const selected = selectLegacyInstall([older, newer]);
    expect(selected.version).toBe("3.5.3");
    expect(selected.uninstallerPath).toBe("C:\\Apps\\radar-new\\uninstall.exe");
    expect(selected.installDir).toBe("C:\\Apps\\radar-new");
  });

  it("detects a legacy install from paths when the display name is localized differently", () => {
    const entry = {
      key: "HKEY_C",
      displayName: "Issue Radar Desktop",
      installLocation: "C:\\Program Files\\wecom-issue-radar",
    };
    expect(entryLooksLikeLegacy(entry)).toBe(true);
  });
});

describe("legacy uninstaller path extraction", () => {
  it("handles quoted, unquoted and MSI uninstall strings", () => {
    expect(
      extractUninstallerPath('"C:\\Apps\\radar\\uninstall.exe" /S'),
    ).toBe("C:\\Apps\\radar\\uninstall.exe");
    expect(extractUninstallerPath("C:\\Apps\\radar\\uninstall.exe /S")).toBe(
      "C:\\Apps\\radar\\uninstall.exe",
    );
    expect(extractUninstallerPath('"C:\\Apps\\My App\\uninstall.exe"')).toBe(
      "C:\\Apps\\My App\\uninstall.exe",
    );
    expect(extractUninstallerPath("MsiExec.exe /X{11111111}")).toBeNull();
    expect(extractUninstallerPath("")).toBeNull();
  });
});

/**
 * Legacy Tauri installation detection and one-shot uninstallation.
 *
 * v4 ships a completely different installer, so the old Tauri program must be
 * detected (Windows uninstall registry + common install directories) and
 * removed by the user. Business data lives in `~/.wecom-issue-radar` and is
 * never touched here.
 */

import { execFile, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

import type { LegacyInstallInfo } from "../../shared/contracts";
import { compareVersions, errorMessage } from "./utils";

const execFileAsync = promisify(execFile);
const UNINSTALL_REGISTRY_KEYS = [
  "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
  "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
  "HKLM\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
];
const LEGACY_DISPLAY_NAME_MARKERS = [
  "企微问题雷达",
  "wecom issue radar",
  "wecom-issue-radar",
];
const LEGACY_PATH_MARKERS = ["企微问题雷达", "wecom-issue-radar", "wecom issue radar"];

export interface LegacyRegistryEntry {
  key: string;
  displayName?: string;
  displayVersion?: string;
  installLocation?: string;
  displayIcon?: string;
  uninstallString?: string;
}

/**
 * Parses `reg query ... /s` output into uninstall entries. Format:
 *
 * HKEY_CURRENT_USER\Software\...\Uninstall\{GUID}
 *     DisplayName    REG_SZ    企微问题雷达
 *     DisplayVersion    REG_SZ    3.5.3
 *     UninstallString    REG_SZ    "C:\...\uninstall.exe"
 */
export function parseRegistryQueryOutput(output: string): LegacyRegistryEntry[] {
  const entries: LegacyRegistryEntry[] = [];
  let current: LegacyRegistryEntry | null = null;
  for (const rawLine of output.split(/\r?\n/)) {
    const line = rawLine.replace(/\s+$/, "");
    if (!line.trim()) continue;
    if (/^\s*HKEY_/i.test(line)) {
      current = { key: line.trim() };
      entries.push(current);
      continue;
    }
    if (!current) continue;
    const match = /^\s+(\S+)\s+(REG_[A-Z_]+)\s+(.*)$/.exec(line);
    if (!match) continue;
    const [, valueName, , valueData] = match;
    const value = valueData.trim();
    switch (valueName.toLowerCase()) {
      case "displayname":
        current.displayName = value;
        break;
      case "displayversion":
        current.displayVersion = value;
        break;
      case "installlocation":
        current.installLocation = value;
        break;
      case "displayicon":
        current.displayIcon = value;
        break;
      case "uninstallstring":
        current.uninstallString = value;
        break;
      default:
        break;
    }
  }
  return entries;
}

function normalized(value: string | undefined): string {
  return (value ?? "").toLowerCase();
}

/** True when an uninstall entry looks like the v3 Tauri application. */
export function entryLooksLikeLegacy(entry: LegacyRegistryEntry): boolean {
  const name = normalized(entry.displayName);
  if (LEGACY_DISPLAY_NAME_MARKERS.some((marker) => name.includes(marker))) {
    return true;
  }
  const paths = [entry.installLocation, entry.displayIcon, entry.uninstallString]
    .map(normalized)
    .filter(Boolean);
  return paths.some((value) =>
    LEGACY_PATH_MARKERS.some((marker) => value.includes(marker)),
  );
}

function stripIconIndex(raw: string): string {
  let value = raw.trim();
  // DisplayIcon is often `"C:\path\app.exe",0`; strip the index before the
  // surrounding quotes.
  const iconIndex = /^(.*?),\s*-?\d+$/.exec(value);
  if (iconIndex) value = iconIndex[1].trim();
  if (value.startsWith('"') && value.endsWith('"')) {
    value = value.slice(1, -1).trim();
  }
  return value;
}

/** Extracts an executable path from a registry `UninstallString`. */
export function extractUninstallerPath(uninstallString: string | undefined): string | null {
  const raw = (uninstallString ?? "").trim();
  if (!raw) return null;
  let executable: string;
  if (raw.startsWith('"')) {
    const closing = raw.indexOf('"', 1);
    executable = closing > 1 ? raw.slice(1, closing) : raw.slice(1);
  } else {
    // Unquoted paths with spaces are ambiguous; the convention is that the
    // uninstaller ends with `.exe`.
    const match = /^(.*?\.exe)(?:\s|$)/i.exec(raw);
    executable = match ? match[1] : raw.split(/\s+/)[0];
  }
  executable = executable.trim();
  if (!executable || !/\.exe$/i.test(executable)) return null;
  if (/msiexec/i.test(path.basename(executable))) return null;
  return executable;
}

function installDirectoryOf(entry: LegacyRegistryEntry): string | undefined {
  const location = entry.installLocation?.trim();
  if (location) return location;
  const icon = stripIconIndex(entry.displayIcon ?? "");
  if (icon) return path.win32.dirname(icon);
  return undefined;
}

/** Selects the newest matching legacy install from parsed registry entries. */
export function selectLegacyInstall(entries: LegacyRegistryEntry[]): LegacyInstallInfo {
  const matches = entries.filter(entryLooksLikeLegacy);
  if (matches.length === 0) {
    return { installed: false };
  }
  const sorted = [...matches].sort((left, right) =>
    compareVersions(right.displayVersion ?? "", left.displayVersion ?? ""),
  );
  const best = sorted[0];
  const version = best.displayVersion?.trim();
  const uninstallerPath = extractUninstallerPath(best.uninstallString) ?? undefined;
  const installDir = installDirectoryOf(best);
  return {
    installed: true,
    ...(version ? { version } : {}),
    ...(uninstallerPath ? { uninstallerPath } : {}),
    ...(installDir ? { installDir } : {}),
  };
}

function commonInstallRoots(): string[] {
  return [
    process.env.LOCALAPPDATA,
    process.env.ProgramFiles,
    process.env["ProgramFiles(x86)"],
    process.env.APPDATA,
  ]
    .filter((root): root is string => typeof root === "string" && root.trim() !== "")
    .map((root) => path.resolve(root));
}

function commonInstallNames(): string[] {
  return ["企微问题雷达", "WeCom Issue Radar", "wecom-issue-radar"];
}

function detectFromDirectories(): LegacyInstallInfo {
  for (const root of commonInstallRoots()) {
    for (const name of commonInstallNames()) {
      const directory = path.join(root, name);
      if (!existsSync(directory)) continue;
      const candidates = ["uninstall.exe", "Uninstall.exe", "unins000.exe"]
        .map((file) => path.join(directory, file))
        .filter((file) => existsSync(file));
      return {
        installed: true,
        installDir: directory,
        ...(candidates[0] ? { uninstallerPath: candidates[0] } : {}),
      };
    }
  }
  return { installed: false };
}

export class MigrateService {
  async checkLegacyInstall(): Promise<LegacyInstallInfo> {
    if (process.platform !== "win32") {
      return { installed: false };
    }
    const outputs: string[] = [];
    for (const key of UNINSTALL_REGISTRY_KEYS) {
      try {
        const { stdout } = await execFileAsync("reg", ["query", key, "/s"], {
          windowsHide: true,
          maxBuffer: 16 * 1024 * 1024,
        });
        outputs.push(stdout);
      } catch {
        // Missing registry keys are expected; keep looking.
      }
    }
    const fromRegistry = selectLegacyInstall(
      parseRegistryQueryOutput(outputs.join("\n")),
    );
    if (fromRegistry.installed) return fromRegistry;
    return detectFromDirectories();
  }

  /**
   * Runs the legacy uninstaller with `/S`. Only the program is removed; the
   * business data root (`~/.wecom-issue-radar`) is left untouched.
   */
  async legacyUninstall(uninstallerPath?: string): Promise<void> {
    if (process.platform !== "win32") {
      throw new Error("仅 Windows 支持卸载旧版应用。");
    }
    let target = uninstallerPath?.trim();
    if (!target) {
      const info = await this.checkLegacyInstall();
      if (!info.installed) {
        throw new Error("未检测到旧版安装，无需卸载。");
      }
      target = info.uninstallerPath?.trim();
      if (!target) {
        throw new Error("未找到旧版卸载程序，请在“设置 → 应用”中手动卸载旧版。");
      }
    }
    const resolved = path.resolve(target);
    try {
      const metadata = await stat(resolved);
      if (!metadata.isFile()) {
        throw new Error("不是文件");
      }
    } catch (error) {
      throw new Error(`找不到旧版卸载程序：${resolved}（${errorMessage(error)}）`);
    }
    if (!/\.exe$/i.test(resolved)) {
      throw new Error("旧版卸载程序路径无效。");
    }
    let child;
    try {
      child = spawn(resolved, ["/S"], {
        detached: true,
        stdio: "ignore",
        windowsHide: false,
      });
    } catch (error) {
      throw new Error(`启动旧版卸载程序失败：${errorMessage(error)}`);
    }
    await new Promise<void>((resolve, reject) => {
      const onSpawn = () => {
        cleanup();
        resolve();
      };
      const onError = (error: Error) => {
        cleanup();
        reject(new Error(`启动旧版卸载程序失败：${errorMessage(error)}`));
      };
      const cleanup = () => {
        child.removeListener("spawn", onSpawn);
        child.removeListener("error", onError);
      };
      child.once("spawn", onSpawn);
      child.once("error", onError);
    });
    child.unref();
  }
}

export function legacyDataRoot(): string {
  return path.join(os.homedir(), ".wecom-issue-radar");
}

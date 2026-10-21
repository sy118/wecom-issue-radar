/** File-manager / documentation opening commands. */

import { spawn } from "node:child_process";
import { mkdir, stat } from "node:fs/promises";
import path from "node:path";

import { shell } from "electron";

import { resolveAgentLogDirectory } from "../../shared/paths";
import { errorMessage } from "./utils";

export const DOCUMENTATION_URL = "https://github.com/sy118/wecom-issue-radar";

/** Builds the platform command used to reveal a file or open a directory. */
export function fileManagerCommand(
  resolvedPath: string,
  isFile: boolean,
  platform: NodeJS.Platform = process.platform,
): { command: string; args: string[] } {
  if (platform === "win32") {
    return {
      command: "explorer.exe",
      args: [isFile ? `/select,${resolvedPath}` : resolvedPath],
    };
  }
  const command = platform === "darwin" ? "open" : "xdg-open";
  return { command, args: [resolvedPath] };
}

export async function openPath(target: string): Promise<void> {
  const trimmed = target?.trim();
  if (!trimmed) {
    throw new Error("请选择要打开的文件或目录");
  }
  const resolved = path.resolve(trimmed);
  let isFile: boolean;
  try {
    const metadata = await stat(resolved);
    isFile = metadata.isFile();
  } catch {
    throw new Error(`文件或目录不存在：${trimmed}`);
  }
  const { command, args } = fileManagerCommand(resolved, isFile);
  await new Promise<void>((resolve, reject) => {
    const child = spawn(command, args, {
      detached: true,
      stdio: "ignore",
      windowsHide: false,
    });
    const onSpawn = () => {
      cleanup();
      child.unref();
      resolve();
    };
    const onError = (error: Error) => {
      cleanup();
      reject(new Error(`打开路径失败：${errorMessage(error)}`));
    };
    const cleanup = () => {
      child.removeListener("spawn", onSpawn);
      child.removeListener("error", onError);
    };
    child.once("spawn", onSpawn);
    child.once("error", onError);
  });
}

/** Creates (if needed) and opens the agent log directory, returning its path. */
export async function openAgentLogDirectory(dataRoot: string): Promise<string> {
  const directory = resolveAgentLogDirectory(dataRoot);
  try {
    await mkdir(directory, { recursive: true });
  } catch (error) {
    throw new Error(`创建 Agent 日志目录失败：${errorMessage(error)}`);
  }
  await openPath(directory);
  return directory;
}

export async function openDocumentation(): Promise<void> {
  try {
    await shell.openExternal(DOCUMENTATION_URL);
  } catch {
    throw new Error("无法打开使用说明，请稍后重试");
  }
}

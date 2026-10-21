/** Atomic JSON file helpers used by the scheduler and UI state stores. */

import { mkdir, open, readFile, rename, rm } from "node:fs/promises";
import { randomBytes } from "node:crypto";
import path from "node:path";

import { errorMessage } from "./utils";

export function isNotFoundError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as NodeJS.ErrnoException).code === "ENOENT"
  );
}

/**
 * Reads and parses a JSON file. Returns null when the file does not exist so
 * callers can apply their own defaults; malformed JSON is reported with the
 * offending path.
 */
export async function readJsonFile<T = unknown>(filePath: string): Promise<T | null> {
  let text: string;
  try {
    text = await readFile(filePath, "utf8");
  } catch (error) {
    if (isNotFoundError(error)) return null;
    throw new Error(`读取文件失败（${filePath}）：${errorMessage(error)}`);
  }
  try {
    return JSON.parse(text) as T;
  } catch (error) {
    throw new Error(`文件不是有效 JSON（${filePath}）：${errorMessage(error)}`);
  }
}

/**
 * Writes pretty-printed JSON through a sibling temporary file followed by an
 * atomic rename, mirroring the previous Rust implementation (temp file + fsync
 * + rename).
 */
export async function atomicWriteJson(filePath: string, value: unknown): Promise<void> {
  const directory = path.dirname(filePath);
  try {
    await mkdir(directory, { recursive: true });
  } catch (error) {
    throw new Error(`创建目录失败（${directory}）：${errorMessage(error)}`);
  }
  const temporaryPath = path.join(
    directory,
    `.${path.basename(filePath)}.${randomBytes(6).toString("hex")}.tmp`,
  );
  const content = `${JSON.stringify(value, null, 2)}\n`;
  let handle: Awaited<ReturnType<typeof open>> | null = null;
  try {
    handle = await open(temporaryPath, "w");
    await handle.writeFile(content, "utf8");
    await handle.sync();
  } catch (error) {
    throw new Error(`写入文件失败（${filePath}）：${errorMessage(error)}`);
  } finally {
    await handle?.close().catch(() => undefined);
  }
  try {
    await rename(temporaryPath, filePath);
  } catch (error) {
    await rm(temporaryPath, { force: true }).catch(() => undefined);
    throw new Error(`替换文件失败（${filePath}）：${errorMessage(error)}`);
  }
}

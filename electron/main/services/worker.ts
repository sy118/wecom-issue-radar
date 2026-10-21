/**
 * One-shot worker process management: temp request file + NDJSON stdout,
 * progress forwarding, lifecycle locking for update installation, and the
 * detached key-extraction console.
 *
 * The request/response shapes mirror the previous Rust `worker.rs` and
 * `commands.rs` implementations so the Python sidecar needs no changes for the
 * command surface itself.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import type { MainContext, SmartSheetPayload, TaskRunRequest } from "../../shared/contracts";
import { resolveWorkerLaunch, workerEnvironment, type WorkerLaunchSpec } from "../../shared/paths";
import {
  collectWorkerOutcome,
  parseWorkerLine,
  workerFailureMessage,
  type WorkerEvent,
} from "../../shared/worker-protocol";
import { terminateProcessTree, waitForExit } from "./process-tree";
import { errorMessage, isRecord, trimmedString } from "./utils";

const KEY_EXTRACTION_FLAG = "--extract-keys";
const REQUEST_FLAG = "--request";
const SHUTDOWN_GRACE_MS = 2_000;

/** Context subset required by the worker client (kept free of Electron imports). */
export interface WorkerServiceContext {
  projectRoot: string;
  resourcesPath: string;
  isPackaged: boolean;
  configPath: string;
  publish: MainContext["publish"];
}

export interface WorkerClientOptions {
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  homeDir?: string;
  tempDirectory?: string;
  shutdownGraceMs?: number;
}

export interface SmartSheetSyncGuard {
  definitionPath: string;
  expectedTemplateRevision: string;
  expectedDocumentRevision: string;
}

function requiredPayloadString(
  payload: Record<string, unknown>,
  key: string,
  errorText: string,
): string {
  const value = trimmedString(payload[key]);
  if (!value) throw new Error(errorText);
  return value;
}

/**
 * Enforces the frozen snapshot/revision guard required before writing to a
 * Tencent document (mirrors `commands::smart_sheet_sync_guard`).
 */
export function smartSheetSyncGuard(payload: Record<string, unknown>): SmartSheetSyncGuard {
  return {
    definitionPath: requiredPayloadString(
      payload,
      "definitionPath",
      "腾讯文档同步必须使用冻结的问题定义快照",
    ),
    expectedTemplateRevision: requiredPayloadString(
      payload,
      "expectedTemplateRevision",
      "腾讯文档同步前必须先刷新预览并确认模板 revision",
    ),
    expectedDocumentRevision: requiredPayloadString(
      payload,
      "expectedDocumentRevision",
      "腾讯文档同步前必须先刷新预览并确认问题快照 revision",
    ),
  };
}

/**
 * A task may only freeze a Smart Sheet template when it prepares one
 * (mirrors `commands::frozen_task_template_id`).
 */
export function frozenTaskTemplateId(request: Record<string, unknown>): string {
  const templateId = trimmedString(request["smartSheetTemplateId"]) ?? "";
  if (request["prepareSmartSheet"] === true && !templateId) {
    throw new Error("准备腾讯文档同步前必须冻结模板 ID");
  }
  return templateId;
}

/** Turns spawn failures into an actionable Chinese message. */
export function workerLaunchErrorMessage(program: string, error: unknown): string {
  const code = isRecord(error) ? error.code : undefined;
  if (code === "ENOENT") {
    return `找不到处理引擎（${program}）。请确认应用已正确安装，或设置 WECOM_ISSUE_RADAR_WORKER 指向处理引擎后重试。`;
  }
  return `无法启动处理引擎（${program}）：${errorMessage(error)}`;
}

/** Pure wrapper around the shared NDJSON failure mapping (kept exported for tests). */
export function describeWorkerFailure(options: {
  outcome: ReturnType<typeof collectWorkerOutcome>;
  exitCode: number | null;
  stderr: string;
}): string | null {
  return workerFailureMessage(options);
}

export class WorkerClient {
  private active = 0;
  private accepting = true;
  private closed = false;
  private readonly children = new Set<ChildProcess>();
  private readonly env: NodeJS.ProcessEnv;
  private readonly platform: NodeJS.Platform;
  private readonly homeDir: string;
  private readonly tempDirectory: string;
  private readonly shutdownGraceMs: number;

  constructor(
    private readonly ctx: WorkerServiceContext,
    options: WorkerClientOptions = {},
  ) {
    this.env = options.env ?? process.env;
    this.platform = options.platform ?? process.platform;
    this.homeDir = options.homeDir ?? os.homedir();
    this.tempDirectory = options.tempDirectory ?? os.tmpdir();
    this.shutdownGraceMs = options.shutdownGraceMs ?? SHUTDOWN_GRACE_MS;
  }

  get activeCount(): number {
    return this.active;
  }

  get isAcceptingWork(): boolean {
    return this.accepting && !this.closed;
  }

  async detectEnvironment(): Promise<unknown> {
    return this.runWorkerAction("detect", {});
  }

  async listGroups(): Promise<unknown> {
    return this.runWorkerAction("groups", {
      configPath: this.ctx.configPath,
      limit: 150,
    });
  }

  async runTask(request: TaskRunRequest): Promise<unknown> {
    return this.runWorkerAction("run", {
      configPath: this.ctx.configPath,
      request,
    });
  }

  async previewSmartSheet(payload: SmartSheetPayload): Promise<unknown> {
    return this.runWorkerAction("preview", {
      configPath: this.ctx.configPath,
      dayDir: payload.dayDir ?? null,
      date: payload.date ?? null,
      templateId: payload.templateId ?? "",
      definitionPath: payload.definitionPath ?? "",
    });
  }

  async syncSmartSheet(payload: SmartSheetPayload): Promise<unknown> {
    const guard = smartSheetSyncGuard(
      payload as unknown as Record<string, unknown>,
    );
    return this.runWorkerAction("sync", {
      configPath: this.ctx.configPath,
      dayDir: payload.dayDir ?? null,
      date: payload.date ?? null,
      uploadImages: payload.uploadImages ?? true,
      templateId: payload.templateId ?? "",
      definitionPath: guard.definitionPath,
      expectedTemplateRevision: guard.expectedTemplateRevision,
      expectedDocumentRevision: guard.expectedDocumentRevision,
    });
  }

  /** Re-scans local WeCom image caches for a finished export (idempotent). */
  async rescanImages(dayDir: string, messageIds: number[] = []): Promise<unknown> {
    return this.runWorkerAction("rescan_images", {
      configPath: this.ctx.configPath,
      dayDir,
      messageIds,
      waitSeconds: 10,
    });
  }

  /**
   * Runs a one-shot worker action. The request travels through a temporary JSON
   * file so large payloads never hit command-line limits; the directory is
   * always removed afterwards.
   */
  async runWorkerAction(
    action: string,
    payload: Record<string, unknown> = {},
  ): Promise<unknown> {
    if (this.closed) {
      throw new Error("应用正在退出，无法启动处理任务。");
    }
    if (!this.accepting) {
      throw new Error("应用正在安装更新，暂时不能启动任务。");
    }
    const request = `${JSON.stringify({ action, payload })}\n`;
    let temporaryDirectory: string;
    try {
      temporaryDirectory = await mkdtemp(path.join(this.tempDirectory, "wecom-issue-radar-"));
    } catch (error) {
      throw new Error(`创建任务请求失败：${errorMessage(error)}`);
    }
    const requestFile = path.join(temporaryDirectory, "request.json");
    try {
      try {
        await writeFile(requestFile, request, "utf8");
      } catch (error) {
        throw new Error(`生成任务请求失败：${errorMessage(error)}`);
      }
      return await this.executeRequest(requestFile);
    } finally {
      await rm(temporaryDirectory, { recursive: true, force: true }).catch(() => undefined);
    }
  }

  /**
   * Blocks new work while the updater installs. Mirrors the Rust lifecycle:
   * active tasks cannot be interrupted, and already-running tasks must finish
   * before the caller is allowed to continue.
   */
  prepareUpdateInstall(): void {
    if (this.closed) {
      throw new Error("应用正在退出，无法安装更新。");
    }
    if (this.active > 0) {
      throw new Error(
        `当前有 ${this.active} 个任务正在运行，请等待任务完成后再安装更新。`,
      );
    }
    this.accepting = false;
  }

  cancelUpdateInstall(): void {
    if (this.closed) return;
    this.accepting = true;
  }

  /** Opens the detached `--extract-keys` console window. */
  async launchKeyExtraction(): Promise<void> {
    if (this.closed) {
      throw new Error("应用正在退出，无法启动密钥提取。");
    }
    if (!this.accepting) {
      throw new Error("应用正在安装更新，暂时不能启动任务。");
    }
    const spec = this.resolveLaunch();
    let child: ChildProcess;
    try {
      child = spawn(spec.program, [...spec.args, KEY_EXTRACTION_FLAG], {
        cwd: spec.cwd,
        env: workerEnvironment(this.env, this.ctx.configPath),
        // On Windows `detached` gives the console child its own window, which
        // is exactly what the key-extraction prompt needs.
        detached: true,
        stdio: "ignore",
        windowsHide: false,
      });
    } catch (error) {
      throw new Error(workerLaunchErrorMessage(spec.program, error));
    }
    this.active += 1;
    this.children.add(child);
    child.once("exit", () => {
      this.active = Math.max(0, this.active - 1);
      this.children.delete(child);
    });
    try {
      await new Promise<void>((resolve, reject) => {
        const onSpawn = () => {
          cleanup();
          resolve();
        };
        const onError = (error: Error) => {
          cleanup();
          this.active = Math.max(0, this.active - 1);
          this.children.delete(child);
          reject(new Error(workerLaunchErrorMessage(spec.program, error)));
        };
        const cleanup = () => {
          child.removeListener("spawn", onSpawn);
          child.removeListener("error", onError);
        };
        child.once("spawn", onSpawn);
        child.once("error", onError);
      });
    } finally {
      // Never keep the application alive for the detached console.
      child.unref();
    }
  }

  /** Forcibly stops every worker child; used on application exit. */
  async shutdown(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.accepting = false;
    const children = [...this.children];
    for (const child of children) {
      terminateProcessTree(child, this.platform);
    }
    await Promise.all(children.map((child) => waitForExit(child, this.shutdownGraceMs)));
    this.children.clear();
    this.active = 0;
  }

  private async executeRequest(requestFile: string): Promise<unknown> {
    const spec = this.resolveLaunch();
    let child: ChildProcess;
    try {
      child = spawn(spec.program, [...spec.args, REQUEST_FLAG, requestFile], {
        cwd: spec.cwd,
        env: workerEnvironment(this.env, this.ctx.configPath),
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      throw new Error(workerLaunchErrorMessage(spec.program, error));
    }
    this.active += 1;
    this.children.add(child);

    const events: WorkerEvent[] = [];
    let stderr = "";
    let stdoutBuffer = "";
    let exitCode: number | null = null;

    const handleEvent = (event: WorkerEvent | null): void => {
      if (!event) return;
      if (event.type === "progress") {
        this.ctx.publish("pipeline-progress", { message: event.message });
      } else if (event.type === "log") {
        // The Rust supervisor forwarded raw non-JSON stdout lines as progress.
        this.ctx.publish("pipeline-progress", { message: event.message });
      }
      events.push(event);
    };

    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      stdoutBuffer += chunk;
      const lines = stdoutBuffer.split(/\r?\n/);
      stdoutBuffer = lines.pop() ?? "";
      for (const line of lines) {
        handleEvent(parseWorkerLine(line));
      }
    });
    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", (chunk: string) => {
      stderr += chunk;
    });

    try {
      const settled = await new Promise<{
        code: number | null;
        spawnError: Error | null;
      }>((resolve) => {
        let finished = false;
        const finish = (code: number | null, spawnError: Error | null) => {
          if (finished) return;
          finished = true;
          resolve({ code, spawnError });
        };
        // `close` fires after stdout/stderr are drained, so no progress line is
        // lost when the process exits quickly.
        child.once("close", (code) => finish(code ?? null, null));
        child.once("error", (error) => finish(null, error));
      });

      if (stdoutBuffer.trim()) {
        handleEvent(parseWorkerLine(stdoutBuffer));
      }
      if (settled.spawnError) {
        throw new Error(workerLaunchErrorMessage(spec.program, settled.spawnError));
      }
      exitCode = settled.code;
    } finally {
      this.children.delete(child);
      this.active = Math.max(0, this.active - 1);
      if (child.exitCode === null && child.signalCode === null) {
        terminateProcessTree(child, this.platform);
      }
    }

    const outcome = collectWorkerOutcome(events);
    const failure = describeWorkerFailure({ outcome, exitCode, stderr });
    if (failure) {
      throw new Error(failure);
    }
    if (outcome.result === undefined) {
      throw new Error("处理引擎没有返回结果");
    }
    return outcome.result;
  }

  private resolveLaunch(): WorkerLaunchSpec {
    return resolveWorkerLaunch({
      homeDir: this.homeDir,
      env: this.env,
      isPackaged: this.ctx.isPackaged,
      resourcesPath: this.ctx.resourcesPath,
      projectRoot: this.ctx.projectRoot,
      platform: this.platform,
    });
  }
}

/**
 * Resident personal-WeChat sidecar supervisor.
 *
 * Protocol (worker/wechat/stdio.py):
 *   stdin:  {"id": "...", "action": "messages", "payload": {...}}
 *   stdout: {"id": "...", "ok": true, "data": ...}
 *           {"id": "...", "ok": false, "error": {"code", "message", "retryable"}}
 *
 * The supervisor starts lazily on the first WeChat request, correlates responses
 * by id, times out stuck requests, and restarts on the next request after a
 * crash. If it cannot start or dies repeatedly the caller falls back to the
 * one-shot --request worker.
 */
import { spawn, type ChildProcess } from "node:child_process";
import os from "node:os";

import { resolveWorkerLaunch, workerEnvironment } from "../../shared/paths";
import { terminateProcessTree, waitForExit } from "./process-tree";
import { errorMessage, isRecord } from "./utils";

const WECHAT_RUNTIME_FLAG = "--wechat-runtime";
const DEFAULT_REQUEST_TIMEOUT_MS = 60_000;
const LONG_REQUEST_TIMEOUT_MS = 600_000;
const LONG_RUNNING_ACTIONS = new Set([
  "bootstrap",
  "draft_generate",
  "generate",
  "draft_rewrite",
  "rewrite",
  "export",
  "export_run",
  "image_key_scan",
  "scan_image_key",
  "scan_keys",
  "unlock",
  "import_proxytalk",
  "import_legacy",
]);
const START_TIMEOUT_MS = 20_000;
const SHUTDOWN_GRACE_MS = 2_000;
const MAX_RESTARTS = 3;

export interface WechatRuntimeContext {
  projectRoot: string;
  resourcesPath: string;
  isPackaged: boolean;
  configPath: string;
}

export interface WechatRuntimeStatus {
  mode: "resident" | "unavailable";
  started: boolean;
  pid: number | null;
  restarts: number;
  inFlight: number;
  lastError: string;
  startupMs: number;
}

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

export class WechatRuntimeUnavailable extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WechatRuntimeUnavailable";
  }
}

export class WechatRuntimeSupervisor {
  private child: ChildProcess | null = null;
  private stdin: NodeJS.WritableStream | null = null;
  private starting: Promise<void> | null = null;
  private stdoutBuffer = "";
  private nextId = 1;
  private readonly pending = new Map<string, PendingRequest>();
  private stopped = false;
  private restarts = 0;
  private lastError = "";
  private startupMs = 0;
  private readonly env: NodeJS.ProcessEnv;
  private readonly platform: NodeJS.Platform;
  private readonly homeDir: string;
  private readonly requestTimeoutMs: number;
  private readonly startTimeoutMs: number;
  private readonly shutdownGraceMs: number;
  private readonly maxRestarts: number;
  private readonly spawnImpl: typeof spawn;

  constructor(
    private readonly ctx: WechatRuntimeContext,
    options: {
      env?: NodeJS.ProcessEnv;
      platform?: NodeJS.Platform;
      homeDir?: string;
      requestTimeoutMs?: number;
      startTimeoutMs?: number;
      shutdownGraceMs?: number;
      maxRestarts?: number;
      spawnImpl?: typeof spawn;
    } = {},
  ) {
    this.env = options.env ?? process.env;
    this.platform = options.platform ?? process.platform;
    this.homeDir = options.homeDir ?? os.homedir();
    this.requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    this.startTimeoutMs = options.startTimeoutMs ?? START_TIMEOUT_MS;
    this.shutdownGraceMs = options.shutdownGraceMs ?? SHUTDOWN_GRACE_MS;
    this.maxRestarts = options.maxRestarts ?? MAX_RESTARTS;
    this.spawnImpl = options.spawnImpl ?? spawn;
  }

  isAvailable(): boolean {
    return !this.stopped && this.restarts < this.maxRestarts;
  }

  status(): WechatRuntimeStatus {
    return {
      mode: this.isAvailable() ? "resident" : "unavailable",
      started: this.child !== null,
      pid: this.child?.pid ?? null,
      restarts: this.restarts,
      inFlight: this.pending.size,
      lastError: this.lastError,
      startupMs: this.startupMs,
    };
  }

  async execute(action: string, payload: Record<string, unknown> = {}): Promise<unknown> {
    if (this.stopped) {
      throw new WechatRuntimeUnavailable("个人微信常驻运行时已经停止。");
    }
    if (!this.isAvailable()) {
      throw new WechatRuntimeUnavailable(
        `个人微信常驻运行时已重启 ${this.restarts} 次仍未恢复：${this.lastError || "未知错误"}`,
      );
    }
    await this.ensureStarted();
    return this.request(action, payload);
  }

  /** Allows a cancelled update to resume the resident worker later. */
  resume(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.restarts = 0;
    this.lastError = "";
  }

  async shutdown(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    this.failPending("应用正在退出，个人微信请求已取消。");
    const child = this.child;
    this.child = null;
    this.stdin = null;
    if (!child) return;
    try {
      child.stdin?.end();
    } catch {
      // ignore broken pipes
    }
    const exited = await waitForExit(child, this.shutdownGraceMs);
    if (!exited) terminateProcessTree(child, this.platform);
  }

  private async ensureStarted(): Promise<void> {
    if (this.child) return;
    if (this.starting) return this.starting;
    this.starting = this.spawnChild().finally(() => {
      this.starting = null;
    });
    return this.starting;
  }

  private async spawnChild(): Promise<void> {
    const startedAt = Date.now();
    const spec = resolveWorkerLaunch({
      homeDir: this.homeDir,
      env: this.env,
      isPackaged: this.ctx.isPackaged,
      resourcesPath: this.ctx.resourcesPath,
      projectRoot: this.ctx.projectRoot,
      platform: this.platform,
    });
    let child: ChildProcess;
    try {
      child = this.spawnImpl(spec.program, [...spec.args, WECHAT_RUNTIME_FLAG], {
        cwd: spec.cwd,
        env: workerEnvironment(this.env, this.ctx.configPath),
        windowsHide: true,
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (error) {
      this.recordFailure(`无法启动个人微信常驻运行时：${errorMessage(error)}`);
      throw new WechatRuntimeUnavailable(this.lastError);
    }
    if (!child.stdin || !child.stdout) {
      terminateProcessTree(child, this.platform);
      this.recordFailure("无法建立个人微信运行时标准输入输出通道。");
      throw new WechatRuntimeUnavailable(this.lastError);
    }
    this.child = child;
    this.stdin = child.stdin;
    this.stdoutBuffer = "";
    child.stdin.on("error", () => {
      // A broken pipe is handled by the exit/fallback path.
    });
    child.stderr?.on("data", () => {
      // Drain stderr so a chatty sidecar cannot block on a full pipe.
    });
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => this.handleStdout(chunk));
    child.once("exit", () => this.handleExit(child));
    try {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(
          () => reject(new Error("常驻运行时启动超时。")),
          this.startTimeoutMs,
        );
        const onSpawn = () => {
          clearTimeout(timer);
          cleanup();
          resolve();
        };
        const onError = (error: Error) => {
          clearTimeout(timer);
          cleanup();
          reject(error);
        };
        const cleanup = () => {
          child.removeListener("spawn", onSpawn);
          child.removeListener("error", onError);
        };
        child.once("spawn", onSpawn);
        child.once("error", onError);
      });
    } catch (error) {
      if (this.child === child) {
        this.child = null;
        this.stdin = null;
      }
      terminateProcessTree(child, this.platform);
      this.recordFailure(`个人微信常驻运行时启动失败：${errorMessage(error)}`);
      throw new WechatRuntimeUnavailable(this.lastError);
    }
    if (this.stopped) {
      if (this.child === child) {
        this.child = null;
        this.stdin = null;
      }
      terminateProcessTree(child, this.platform);
      throw new WechatRuntimeUnavailable("个人微信常驻运行时在启动过程中被关闭。");
    }
    this.startupMs = Date.now() - startedAt;
  }

  private request(action: string, payload: Record<string, unknown>): Promise<unknown> {
    const child = this.child;
    const stdin = this.stdin;
    if (!child || !stdin) {
      return Promise.reject(new WechatRuntimeUnavailable("个人微信常驻运行时尚未就绪。"));
    }
    const id = String(this.nextId++);
    const timeout = LONG_RUNNING_ACTIONS.has(action)
      ? LONG_REQUEST_TIMEOUT_MS
      : this.requestTimeoutMs;
    return new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`个人微信操作超时（${action}）。`));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      try {
        stdin.write(`${JSON.stringify({ id, action, payload })}\n`);
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error(`无法写入个人微信常驻运行时：${errorMessage(error)}`));
      }
    });
  }

  private handleStdout(chunk: string): void {
    this.stdoutBuffer += chunk;
    const lines = this.stdoutBuffer.split(/\r?\n/);
    this.stdoutBuffer = lines.pop() ?? "";
    for (const line of lines) {
      this.handleLine(line);
    }
  }

  private handleLine(line: string): void {
    const text = line.trim();
    if (!text) return;
    let message: unknown;
    try {
      message = JSON.parse(text);
    } catch {
      this.lastError = "个人微信常驻运行时返回了非法 JSON。";
      return;
    }
    if (!isRecord(message) || typeof message.id !== "string") return;
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    clearTimeout(pending.timer);
    if (message.ok === true) {
      pending.resolve(message.data);
      return;
    }
    const error = isRecord(message.error) ? message.error : {};
    const detail = new Error(
      typeof error.message === "string" && error.message
        ? error.message
        : "个人微信操作失败。",
    );
    detail.name = typeof error.code === "string" ? error.code : "WECHAT_ACTION_FAILED";
    pending.reject(detail);
  }

  private handleExit(child: ChildProcess): void {
    if (this.child !== child) return;
    this.child = null;
    this.stdin = null;
    this.failPending("个人微信常驻运行时已退出，请重试。");
    if (!this.stopped) {
      this.restarts += 1;
      this.lastError = `常驻运行时意外退出（第 ${this.restarts} 次）。`;
    }
  }

  private failPending(message: string): void {
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(new Error(message));
      this.pending.delete(id);
    }
  }

  private recordFailure(message: string): void {
    this.lastError = message;
    this.restarts += 1;
  }
}

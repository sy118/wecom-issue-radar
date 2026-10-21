/**
 * Resident reply-runtime sidecar supervisor.
 *
 * Wire protocol (identical to the Rust implementation and to
 * `worker/reply_runtime/stdio.py`):
 *   stdin:  {id, op: "execute" | "query", payload}
 *   stdout: {id, ok: true, data} | {id, ok: false, error}
 *           {type: "event", seq, event}
 *
 * Features: request/response correlation by id, 600 s default timeout,
 * strictly-increasing event sequence forwarding, recursive secret redaction,
 * crash restart with 100 ms → 5 s exponential backoff, and the update pause /
 * resume interlock used before installing an update.
 */

import { spawn, type ChildProcess } from "node:child_process";
import os from "node:os";

import type { MainContext } from "../../shared/contracts";
import { resolveWorkerLaunch, workerEnvironment, type WorkerLaunchSpec } from "../../shared/paths";
import { terminateProcessTree, waitForExit } from "./process-tree";
import { errorMessage, isRecord } from "./utils";

export const DEFAULT_REQUEST_TIMEOUT_MS = 600_000;
export const DEFAULT_SHUTDOWN_GRACE_MS = 2_000;
export const INITIAL_RESTART_DELAY_MS = 100;
export const MAX_RESTART_DELAY_MS = 5_000;

const REPLY_RUNTIME_FLAG = "--reply-runtime";
const SECRET_KEY_PATTERN = /(apikey|secret|token|password|authorization|webhook)/;
const REDACTED = "[redacted]";

export interface ReplyRuntimeContext {
  projectRoot: string;
  resourcesPath: string;
  isPackaged: boolean;
  configPath: string;
  publish: MainContext["publish"];
}

export interface ReplyRuntimeOptions {
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  homeDir?: string;
  requestTimeoutMs?: number;
  shutdownGraceMs?: number;
  initialRestartDelayMs?: number;
  maxRestartDelayMs?: number;
  logger?: (message: string) => void;
}

/** Structured, user-facing runtime error (serialized by the IPC layer). */
export class ReplyRuntimeRequestError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly retryable: boolean,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = "ReplyRuntimeRequestError";
  }
}

export function normalizeSecretKey(key: string): string {
  return key.toLowerCase().replace(/[^a-z0-9]/g, "");
}

export function isSecretKey(key: string): boolean {
  return SECRET_KEY_PATTERN.test(normalizeSecretKey(key));
}

/** Recursively replaces secret-looking values with `[redacted]` (never mutates). */
export function redactSecrets(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => redactSecrets(item));
  }
  if (isRecord(value)) {
    const redacted: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(value)) {
      redacted[key] = isSecretKey(key) ? REDACTED : redactSecrets(child);
    }
    return redacted;
  }
  return value;
}

export interface RoutedRuntimeEvent {
  forwarded: boolean;
  lastSeq: number;
  payload: Record<string, unknown> | null;
}

/**
 * Pure event router: forwards only strictly-increasing event sequences and
 * redacts nested secrets. Also used by the supervisor so the dedupe/redaction
 * rules can be tested without spawning Python.
 */
export function routeRuntimeEvent(
  message: unknown,
  lastSeq: number,
): RoutedRuntimeEvent {
  if (!isRecord(message) || message.type !== "event") {
    return { forwarded: false, lastSeq, payload: null };
  }
  const seq =
    typeof message.seq === "number" && Number.isFinite(message.seq)
      ? Math.trunc(message.seq)
      : null;
  if (seq === null || seq <= lastSeq) {
    return { forwarded: false, lastSeq, payload: null };
  }
  const event = isRecord(message.event) ? message.event : {};
  return {
    forwarded: true,
    lastSeq: seq,
    payload: {
      type: "event",
      seq,
      event: redactSecrets(event) as Record<string, unknown>,
    },
  };
}

interface PendingRequest {
  resolve(value: unknown): void;
  reject(error: ReplyRuntimeRequestError): void;
  timer: NodeJS.Timeout;
}

function workerError(error: unknown): ReplyRuntimeRequestError {
  if (typeof error === "string") {
    return new ReplyRuntimeRequestError("RUNTIME_REQUEST_FAILED", error, false);
  }
  if (!isRecord(error)) {
    return new ReplyRuntimeRequestError(
      "RUNTIME_PROTOCOL_ERROR",
      "回复运行时返回失败但没有错误信息",
      true,
    );
  }
  return new ReplyRuntimeRequestError(
    typeof error.code === "string" ? error.code : "RUNTIME_REQUEST_FAILED",
    typeof error.message === "string" ? error.message : "回复运行时请求失败",
    error.retryable === true,
    error.details,
  );
}

export class ReplyRuntimeSupervisor {
  private child: ChildProcess | null = null;
  private stdin: NodeJS.WritableStream | null = null;
  private readonly pending = new Map<string, PendingRequest>();
  private lastEventSeq = 0;
  private nextId = 1;
  private started = false;
  private stopped = false;
  private paused = false;
  private launching: Promise<void> | null = null;
  private restartTimer: NodeJS.Timeout | null = null;
  private restartDelay: number;
  private stdoutBuffer = "";
  private shutdownPromise: Promise<void> | null = null;

  private readonly env: NodeJS.ProcessEnv;
  private readonly platform: NodeJS.Platform;
  private readonly homeDir: string;
  private readonly requestTimeoutMs: number;
  private readonly shutdownGraceMs: number;
  private readonly initialRestartDelayMs: number;
  private readonly maxRestartDelayMs: number;
  private readonly logger: (message: string) => void;

  constructor(
    private readonly ctx: ReplyRuntimeContext,
    options: ReplyRuntimeOptions = {},
  ) {
    this.env = options.env ?? process.env;
    this.platform = options.platform ?? process.platform;
    this.homeDir = options.homeDir ?? os.homedir();
    this.requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    this.shutdownGraceMs = options.shutdownGraceMs ?? DEFAULT_SHUTDOWN_GRACE_MS;
    this.initialRestartDelayMs =
      options.initialRestartDelayMs ?? INITIAL_RESTART_DELAY_MS;
    this.maxRestartDelayMs = options.maxRestartDelayMs ?? MAX_RESTART_DELAY_MS;
    this.restartDelay = this.initialRestartDelayMs;
    this.logger = options.logger ?? ((message) => console.error(message));
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    this.stopped = false;
    void this.launchSafely();
  }

  execute(command: Record<string, unknown>): Promise<unknown> {
    return this.request("execute", command);
  }

  query(query: Record<string, unknown>): Promise<unknown> {
    return this.request("query", query);
  }

  /**
   * Stops the sidecar before an update is installed: refuses new requests and
   * waits up to 2 s for a graceful exit before force-killing the process tree.
   */
  async prepareForUpdate(): Promise<void> {
    if (this.stopped) {
      throw new ReplyRuntimeRequestError(
        "RUNTIME_STOPPED",
        "回复运行时已经停止",
        false,
      );
    }
    if (this.paused) {
      throw new ReplyRuntimeRequestError(
        "RUNTIME_BUSY",
        "回复运行时已经在准备应用更新",
        true,
      );
    }
    this.paused = true;
    this.cancelRestart();
    // Wait for an in-flight launch to settle; spawnChild aborts if it observes
    // the pause flag, so no sidecar starts after the update gate closed.
    const launching = this.launching;
    if (launching) {
      await launching.catch(() => undefined);
    }
    this.failPending(
      new ReplyRuntimeRequestError(
        "RUNTIME_PAUSED",
        "回复运行时正在准备应用更新",
        true,
      ),
    );
    await this.stopChild(this.shutdownGraceMs);
  }

  /** Restarts the sidecar after an update was cancelled or failed. */
  async resumeAfterUpdate(): Promise<void> {
    if (this.stopped) {
      throw new ReplyRuntimeRequestError(
        "RUNTIME_STOPPED",
        "回复运行时已经停止",
        false,
      );
    }
    this.paused = false;
    this.restartDelay = this.initialRestartDelayMs;
    void this.launchSafely();
  }

  /** Idempotent shutdown used on application exit. */
  shutdown(): Promise<void> {
    if (!this.shutdownPromise) {
      this.shutdownPromise = this.doShutdown();
    }
    return this.shutdownPromise;
  }

  private async doShutdown(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    this.paused = true;
    this.cancelRestart();
    const launching = this.launching;
    if (launching) {
      await launching.catch(() => undefined);
    }
    this.failPending(
      new ReplyRuntimeRequestError("RUNTIME_STOPPED", "应用正在退出", false),
    );
    await this.stopChild(this.shutdownGraceMs);
  }

  private request(
    operation: "execute" | "query",
    payload: Record<string, unknown>,
  ): Promise<unknown> {
    return this.ensureRunning().then(
      (child) =>
        new Promise<unknown>((resolve, reject) => {
          if (this.stopped) {
            reject(
              new ReplyRuntimeRequestError(
                "RUNTIME_STOPPED",
                "回复运行时已经停止",
                false,
              ),
            );
            return;
          }
          const id = String(this.nextId);
          this.nextId += 1;
          const timer = setTimeout(() => {
            this.pending.delete(id);
            reject(
              new ReplyRuntimeRequestError(
                "RUNTIME_TIMEOUT",
                "回复运行时请求超时，请稍后重试",
                true,
              ),
            );
          }, this.requestTimeoutMs);
          timer.unref?.();
          this.pending.set(id, { resolve, reject, timer });
          const line = `${JSON.stringify({ id, op: operation, payload })}\n`;
          const stdin = this.stdin;
          if (!stdin) {
            this.pending.delete(id);
            clearTimeout(timer);
            reject(this.unavailable("回复运行时连接已断开"));
            return;
          }
          try {
            stdin.write(line);
          } catch (error) {
            this.pending.delete(id);
            clearTimeout(timer);
            reject(
              this.unavailable(`无法写入回复运行时：${errorMessage(error)}`),
            );
            this.restartChild(child);
          }
        }),
    );
  }

  private async ensureRunning(): Promise<ChildProcess> {
    if (this.stopped) {
      throw new ReplyRuntimeRequestError(
        "RUNTIME_STOPPED",
        "回复运行时已经停止",
        false,
      );
    }
    if (this.paused) {
      throw new ReplyRuntimeRequestError(
        "RUNTIME_PAUSED",
        "回复运行时已为应用更新暂停",
        true,
      );
    }
    if (this.child && this.child.exitCode === null) {
      return this.child;
    }
    try {
      await this.launch();
    } catch (error) {
      throw this.asRequestError(error);
    }
    if (!this.child || this.child.exitCode !== null) {
      throw this.unavailable("回复运行时正在重启，请稍后重试");
    }
    return this.child;
  }

  private launchSafely(): Promise<void> {
    return this.launch().catch((error) => {
      this.logger(
        `回复运行时启动失败：${error instanceof Error ? error.message : errorMessage(error)}`,
      );
    });
  }

  private launch(): Promise<void> {
    if (this.launching) return this.launching;
    if (this.stopped || this.paused) {
      return Promise.reject(
        new ReplyRuntimeRequestError("RUNTIME_STOPPED", "回复运行时已经停止", false),
      );
    }
    if (this.child && this.child.exitCode === null) {
      return Promise.resolve();
    }
    let promise: Promise<void>;
    promise = this.spawnChild()
      .catch((error) => {
        this.scheduleRestart();
        throw this.asRequestError(error);
      })
      .finally(() => {
        if (this.launching === promise) {
          this.launching = null;
        }
      });
    this.launching = promise;
    return promise;
  }

  private async spawnChild(): Promise<void> {
    const spec = this.resolveLaunch();
    let child: ChildProcess;
    try {
      child = spawn(spec.program, [...spec.args, REPLY_RUNTIME_FLAG], {
        cwd: spec.cwd,
        env: workerEnvironment(this.env, this.ctx.configPath),
        windowsHide: true,
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (error) {
      throw this.unavailable(
        `无法启动回复运行时（${spec.program}）：${errorMessage(error)}`,
      );
    }
    if (!child.stdin || !child.stdout) {
      terminateProcessTree(child, this.platform);
      throw this.unavailable("无法建立回复运行时标准输入输出通道");
    }
    this.child = child;
    this.stdin = child.stdin;
    this.stdoutBuffer = "";
    child.stdin.on("error", () => {
      // Broken pipe is handled by the write path / restart logic.
    });
    child.stderr?.on("data", () => {
      // Drain stderr so a chatty sidecar cannot block on a full pipe.
    });
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => this.handleStdout(child, chunk));
    child.once("exit", () => this.handleExit(child));
    try {
      await new Promise<void>((resolve, reject) => {
        const onSpawn = () => {
          cleanup();
          resolve();
        };
        const onError = (error: Error) => {
          cleanup();
          reject(this.unavailable(`无法启动回复运行时：${errorMessage(error)}`));
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
      throw error;
    }
    if (this.stopped || this.paused) {
      if (this.child === child) {
        this.child = null;
        this.stdin = null;
      }
      terminateProcessTree(child, this.platform);
      throw this.unavailable("回复运行时启动已为应用更新暂停");
    }
    this.restartDelay = this.initialRestartDelayMs;
  }

  private handleStdout(child: ChildProcess, chunk: string): void {
    if (this.child !== child) return;
    this.stdoutBuffer += chunk;
    const lines = this.stdoutBuffer.split(/\r?\n/);
    this.stdoutBuffer = lines.pop() ?? "";
    for (const line of lines) {
      this.handleLine(line);
    }
  }

  private handleLine(rawLine: string): void {
    const line = rawLine.trim();
    if (!line) return;
    let message: unknown;
    try {
      message = JSON.parse(line);
    } catch {
      this.protocolFailure();
      return;
    }
    if (!isRecord(message)) {
      this.protocolFailure();
      return;
    }
    if (message.type === "event") {
      const routed = routeRuntimeEvent(message, this.lastEventSeq);
      this.lastEventSeq = routed.lastSeq;
      if (routed.forwarded && routed.payload) {
        this.ctx.publish("reply-runtime-event", routed.payload);
      }
      return;
    }
    const id =
      typeof message.id === "string"
        ? message.id
        : typeof message.id === "number"
          ? String(message.id)
          : null;
    if (!id) return;
    const pending = this.pending.get(id);
    if (!pending) return;
    this.pending.delete(id);
    clearTimeout(pending.timer);
    if (message.ok === true) {
      pending.resolve(message.data ?? null);
      return;
    }
    if (message.ok === false) {
      pending.reject(workerError(message.error));
      return;
    }
    pending.reject(
      new ReplyRuntimeRequestError(
        "RUNTIME_PROTOCOL_ERROR",
        "回复运行时响应缺少布尔类型的 ok 字段",
        true,
      ),
    );
  }

  private protocolFailure(): void {
    this.failPending(
      new ReplyRuntimeRequestError(
        "RUNTIME_PROTOCOL_ERROR",
        "回复运行时输出了无效 NDJSON",
        true,
      ),
    );
    const child = this.child;
    if (child) {
      this.restartChild(child);
    }
  }

  private handleExit(child: ChildProcess): void {
    if (this.child !== child) return;
    this.child = null;
    this.stdin = null;
    this.failPending(this.unavailable("回复运行时意外退出，请重试"));
    if (!this.stopped && !this.paused) {
      this.scheduleRestart();
    }
  }

  private restartChild(child: ChildProcess): void {
    if (this.child === child) {
      this.child = null;
      this.stdin = null;
    }
    terminateProcessTree(child, this.platform);
    if (!this.stopped && !this.paused) {
      this.scheduleRestart();
    }
  }

  private scheduleRestart(): void {
    if (this.stopped || this.paused || this.restartTimer) return;
    const delay = Math.min(this.restartDelay, this.maxRestartDelayMs);
    this.restartDelay = Math.min(
      Math.floor(this.restartDelay * 2),
      this.maxRestartDelayMs,
    );
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      if (!this.stopped && !this.paused) {
        void this.launchSafely();
      }
    }, delay);
    this.restartTimer.unref?.();
  }

  private cancelRestart(): void {
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
  }

  private async stopChild(graceMs: number): Promise<void> {
    const child = this.child;
    const stdin = this.stdin;
    this.child = null;
    this.stdin = null;
    if (!child) return;
    try {
      stdin?.end();
    } catch {
      // Ignore: the pipe may already be closed.
    }
    const exited = await waitForExit(child, graceMs);
    if (!exited) {
      terminateProcessTree(child, this.platform);
      await waitForExit(child, 1_000);
    }
  }

  private failPending(error: ReplyRuntimeRequestError): void {
    for (const [id, pending] of this.pending) {
      this.pending.delete(id);
      clearTimeout(pending.timer);
      pending.reject(error);
    }
  }

  private unavailable(message: string): ReplyRuntimeRequestError {
    return new ReplyRuntimeRequestError("RUNTIME_UNAVAILABLE", message, true);
  }

  private asRequestError(error: unknown): ReplyRuntimeRequestError {
    if (error instanceof ReplyRuntimeRequestError) return error;
    return this.unavailable(`回复运行时不可用：${errorMessage(error)}`);
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

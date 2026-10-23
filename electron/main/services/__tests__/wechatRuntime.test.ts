import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import type { ChildProcess } from "node:child_process";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../shared/paths", () => ({
  resolveWorkerLaunch: () => ({ program: "fake-worker", args: [], cwd: "." }),
  workerEnvironment: (env: NodeJS.ProcessEnv) => env,
}));

import { WechatRuntimeSupervisor, WechatRuntimeUnavailable } from "../wechatRuntime";

class FakeChild extends EventEmitter {
  pid = 4242;
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
  readonly stdin = new PassThrough();
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  failWrites = false;

  constructor() {
    super();
    this.stdin.on("data", (chunk: Buffer | string) => {
      if (this.failWrites) return;
      for (const line of chunk.toString().split("\n")) {
        if (!line.trim()) continue;
        const request = JSON.parse(line) as { id: string; action: string; payload: unknown };
        if (request.action === "hang") continue;
        if (request.action === "crash") {
          this.exitCode = 1;
          this.emit("exit", 1);
          continue;
        }
        this.stdout.write(`${JSON.stringify({
          id: request.id,
          ok: true,
          data: { action: request.action, payload: request.payload },
        })}\n`);
      }
    });
    this.stdin.on("finish", () => {
      this.exitCode = 0;
      this.emit("exit", 0);
    });
  }

  kill(): boolean {
    return true;
  }
}

function createSupervisor() {
  const children: FakeChild[] = [];
  const supervisor = new WechatRuntimeSupervisor(
    {
      projectRoot: "C:/repo",
      resourcesPath: "C:/repo/resources",
      isPackaged: false,
      configPath: "C:/config.json",
    },
    {
      requestTimeoutMs: 200,
      startTimeoutMs: 1_000,
      shutdownGraceMs: 50,
      spawnImpl: (() => {
        const child = new FakeChild();
        children.push(child);
        setImmediate(() => child.emit("spawn"));
        return child as unknown as ChildProcess;
      }) as never,
    },
  );
  return { supervisor, children };
}

describe("WechatRuntimeSupervisor", () => {
  const supervisors: WechatRuntimeSupervisor[] = [];

  afterEach(async () => {
    await Promise.all(supervisors.splice(0).map((item) => item.shutdown()));
  });

  it("correlates two concurrent requests and keeps one process", async () => {
    const { supervisor, children } = createSupervisor();
    supervisors.push(supervisor);

    const [first, second] = await Promise.all([
      supervisor.execute("projects", { page: 1 }),
      supervisor.execute("settings", { page: 2 }),
    ]);

    expect(first).toEqual({ action: "projects", payload: { page: 1 } });
    expect(second).toEqual({ action: "settings", payload: { page: 2 } });
    expect(children).toHaveLength(1);
    expect(supervisor.status()).toMatchObject({ mode: "resident", started: true, restarts: 0 });
  });

  it("times out a stuck request", async () => {
    const { supervisor } = createSupervisor();
    supervisors.push(supervisor);

    await expect(supervisor.execute("hang", {})).rejects.toThrow("超时");
  });

  it("restarts on the next request after a crash", async () => {
    const { supervisor, children } = createSupervisor();
    supervisors.push(supervisor);

    await expect(supervisor.execute("crash", {})).rejects.toThrow();
    expect(supervisor.status().restarts).toBeGreaterThan(0);

    await expect(supervisor.execute("settings", {})).resolves.toMatchObject({ action: "settings" });
    expect(children).toHaveLength(2);
  });

  it("shuts down and rejects new work", async () => {
    const { supervisor } = createSupervisor();
    supervisors.push(supervisor);

    await supervisor.execute("settings", {});
    await supervisor.shutdown();

    await expect(supervisor.execute("settings", {})).rejects.toBeInstanceOf(
      WechatRuntimeUnavailable,
    );
    expect(supervisor.status().mode).toBe("unavailable");
  });
});

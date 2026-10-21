/** Process-tree termination helpers (Windows needs `taskkill /T`). */

import { spawn, type ChildProcess } from "node:child_process";

/**
 * Returns true when the process already exited or exits within the timeout.
 * The timeout arm intentionally does not reject: callers decide whether to
 * escalate to a forced kill.
 */
export function waitForExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve(true);
  }
  return new Promise<boolean>((resolve) => {
    let settled = false;
    const settle = (exited: boolean) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener("exit", onExit);
      resolve(exited);
    };
    const onExit = () => settle(true);
    const timer = setTimeout(() => settle(false), Math.max(0, timeoutMs));
    timer.unref?.();
    child.once("exit", onExit);
  });
}

/**
 * Forcibly terminates a child process and its descendants. Windows uses
 * `taskkill /PID <pid> /T /F` so PyInstaller's parent/child pair does not leave
 * an orphan; other platforms use SIGKILL.
 */
export function terminateProcessTree(child: ChildProcess, platform: NodeJS.Platform = process.platform): void {
  const pid = child.pid;
  if (!pid || child.exitCode !== null || child.signalCode !== null) {
    return;
  }
  if (platform === "win32") {
    try {
      const killer = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore",
      });
      killer.once("error", () => {
        // taskkill is generally present on Windows; fall back to a direct kill.
        try {
          child.kill();
        } catch {
          // The process may have exited between the check and the kill.
        }
      });
      killer.unref();
      return;
    } catch {
      // Fall through to a direct kill.
    }
  }
  try {
    child.kill("SIGKILL");
  } catch {
    // The process may already be gone.
  }
}

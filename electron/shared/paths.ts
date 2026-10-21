import path from "node:path";

export interface PathEnvironment {
  homeDir: string;
  env: NodeJS.ProcessEnv;
  isPackaged: boolean;
  resourcesPath: string;
  projectRoot: string;
  platform?: NodeJS.Platform;
}

export function resolveDataRoot(options: Pick<PathEnvironment, "homeDir" | "env">): string {
  const explicit = options.env.WECOM_ISSUE_RADAR_HOME?.trim();
  if (explicit) return path.resolve(explicit);
  return path.join(options.homeDir, ".wecom-issue-radar");
}

export function resolveConfigPath(dataRoot: string, env: NodeJS.ProcessEnv): string {
  const explicit = env.WECOM_ISSUE_RADAR_CONFIG?.trim();
  if (explicit) return path.resolve(explicit);
  return path.join(dataRoot, "config.local.json");
}

export function resolveRuntimeDatabasePath(dataRoot: string, env: NodeJS.ProcessEnv): string {
  const explicit = env.WECOM_ISSUE_RADAR_RUNTIME_DB?.trim();
  if (explicit) return path.resolve(explicit);
  return path.join(dataRoot, "reply-runtime.sqlite3");
}

export function resolveScheduleStatePath(dataRoot: string, env: NodeJS.ProcessEnv): string {
  const explicit = env.WECOM_ISSUE_RADAR_SCHEDULE_STATE?.trim();
  if (explicit) return path.resolve(explicit);
  return path.join(dataRoot, "schedule-state.json");
}

export function resolveAgentLogDirectory(dataRoot: string): string {
  return path.join(dataRoot, "logs", "agent");
}

export interface WorkerLaunchSpec {
  program: string;
  args: string[];
  cwd: string;
}

function workerExecutableName(platform: NodeJS.Platform): string {
  return platform === "win32" ? "wecom-issue-radar-worker.exe" : "wecom-issue-radar-worker";
}

/**
 * Resolves how to start the Python sidecar.
 *
 * Packaged builds always use the PyInstaller executable shipped in
 * `resources/bin`. Development falls back to the repository virtualenv and
 * finally to whatever `python` is on PATH (CI and user machines may not have
 * one; that is reported by the caller as an actionable error).
 */
export function resolveWorkerLaunch(options: PathEnvironment): WorkerLaunchSpec {
  const override = options.env.WECOM_ISSUE_RADAR_WORKER?.trim();
  if (override) {
    return {
      program: path.resolve(override),
      args: [],
      cwd: path.dirname(path.resolve(override)),
    };
  }

  const platform = options.platform ?? "win32";
  if (options.isPackaged) {
    const directory = path.join(options.resourcesPath, "bin");
    return {
      program: path.join(directory, workerExecutableName(platform)),
      args: [],
      cwd: directory,
    };
  }

  const script = path.join(options.projectRoot, "worker", "main.py");
  const windowsPython = path.join(options.projectRoot, ".venv", "Scripts", "python.exe");
  const posixPython = path.join(options.projectRoot, ".venv", "bin", "python");
  const program =
    platform === "win32"
      ? windowsPython
      : posixPython;
  return {
    program,
    args: [script],
    cwd: options.projectRoot,
  };
}

export function workerEnvironment(baseEnv: NodeJS.ProcessEnv, configPath: string): NodeJS.ProcessEnv {
  return {
    ...baseEnv,
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
    WECOM_ISSUE_RADAR_CONFIG: configPath,
  };
}

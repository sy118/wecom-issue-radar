/** Small dependency-free helpers shared by the main-process service layer. */

export function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === "string") {
    return error;
  }
  if (error && typeof error === "object") {
    const record = error as Record<string, unknown>;
    for (const key of ["message", "error", "reason", "detail"]) {
      const nested = record[key];
      if (typeof nested === "string" && nested.trim()) {
        return nested;
      }
    }
  }
  return String(error);
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function asRecord(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}

export function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function trimmedString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function asNonNegativeInteger(value: unknown): number | null {
  const numeric = asNumber(value);
  if (numeric === null || numeric < 0) return null;
  return Math.trunc(numeric);
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/** Parses `1.2.3` style versions; non-numeric segments count as zero. */
export function versionParts(value: string): number[] {
  const core = value.trim().replace(/^v/i, "").split(/[-+]/)[0] ?? "";
  return core.split(".").map((segment) => {
    const match = segment.match(/\d+/);
    return match ? Number.parseInt(match[0], 10) : 0;
  });
}

/** Returns a positive number when `left` is newer than `right`. */
export function compareVersions(left: string, right: string): number {
  const leftParts = versionParts(left);
  const rightParts = versionParts(right);
  const length = Math.max(leftParts.length, rightParts.length);
  for (let index = 0; index < length; index += 1) {
    const delta = (leftParts[index] ?? 0) - (rightParts[index] ?? 0);
    if (delta !== 0) return delta;
  }
  return 0;
}

export function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, Math.max(0, milliseconds));
    timer.unref?.();
  });
}

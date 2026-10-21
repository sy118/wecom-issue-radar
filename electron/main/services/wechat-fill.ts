import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";

export interface FillResult {
  filled: boolean;
  chatTitle: string;
  characters: number;
}

export interface FillContext {
  isPackaged: boolean;
  resourcesPath: string;
  projectRoot: string;
}

function candidateHelpers(context: FillContext): string[] {
  const override = process.env.WECHAT_INPUT_HELPER?.trim();
  const names = ["wechat-input-helper.exe", "wechat-input-helper"];
  const candidates: string[] = [];
  if (override) candidates.push(override);
  if (context.isPackaged) {
    for (const name of names) candidates.push(path.join(context.resourcesPath, "bin", name));
  } else {
    const targetRoot = path.join(context.projectRoot, "native", "wechat-input-helper", "target");
    for (const name of names) {
      candidates.push(path.join(targetRoot, "release", name));
      candidates.push(path.join(targetRoot, "x86_64-pc-windows-msvc", "release", name));
    }
  }
  return candidates;
}

export function resolveHelper(context: FillContext): string {
  const candidate = candidateHelpers(context).find((file) => existsSync(file));
  if (!candidate) {
    throw new Error(
      "未找到微信填入组件（wechat-input-helper.exe）。请先复制草稿，或重新安装包含该组件的版本。",
    );
  }
  return candidate;
}

interface HelperResponse {
  filled?: boolean;
  filled_ok?: boolean;
  chatTitle?: string;
  chat_title?: string;
  characters?: number;
  error?: string;
}

function parseHelperOutput(stdout: string): HelperResponse {
  const lines = stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      const value = JSON.parse(lines[index]);
      if (value && typeof value === "object") return value as HelperResponse;
    } catch {
      // keep looking for the JSON line
    }
  }
  return {};
}

/**
 * Fills the WeChat message editor through the native helper. The helper owns
 * the eight-second focus/title/empty-editor validation chain; this function
 * never sends the message.
 */
export async function fillWechatInput(
  context: FillContext,
  chatTitle: string,
  text: string,
): Promise<FillResult> {
  const title = chatTitle.trim();
  const content = text.trim();
  if (!title || !content) {
    throw new Error("目标聊天标题和草稿内容不能为空。");
  }
  const helper = resolveHelper(context);
  return await new Promise<FillResult>((resolve, reject) => {
    const child = spawn(helper, [], {
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill();
      reject(new Error("填入微信超时（15 秒）。请改用复制。"));
    }, 15_000);

    const finish = (error: Error | null, result?: FillResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else if (result) resolve(result);
    };

    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf-8");
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf-8");
    });
    child.on("error", (error) => {
      finish(new Error(`无法启动微信填入组件：${error.message}`));
    });
    child.on("close", () => {
      const response = parseHelperOutput(stdout);
      const filled = response.filled ?? response.filled_ok ?? false;
      if (filled) {
        finish(null, {
          filled: true,
          chatTitle: String(response.chatTitle ?? response.chat_title ?? title),
          characters: Number(response.characters ?? content.length),
        });
        return;
      }
      const detail = response.error
        || stderr.trim()
        || "未在 8 秒内确认目标聊天和空白输入框，已拒绝填入；请使用复制。";
      finish(new Error(detail));
    });

    if (!child.stdin) {
      finish(new Error("无法向微信填入组件写入请求。"));
      return;
    }
    child.stdin.write(`${JSON.stringify({ chatTitle: title, text: content })}\n`);
    child.stdin.end();
  });
}

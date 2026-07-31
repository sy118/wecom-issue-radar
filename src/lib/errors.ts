const DEFAULT_MESSAGE = "操作没有完成，请稍后重试。";
const MAX_MESSAGE_LENGTH = 120;

const preferredObjectKeys = ["message", "error", "reason", "detail"] as const;

export function errorHasCode(
  value: unknown,
  expectedCode: string,
  seen = new Set<object>(),
): boolean {
  const normalizedCode = expectedCode.trim().toUpperCase();
  if (!normalizedCode) return false;
  if (typeof value === "string") {
    if (value.toUpperCase().includes(normalizedCode)) return true;
    if (!/^\s*[{[]/.test(value)) return false;
    try {
      return errorHasCode(JSON.parse(value), normalizedCode, seen);
    } catch {
      return false;
    }
  }
  if (!value || typeof value !== "object" || seen.has(value)) return false;
  seen.add(value);
  const record = value as Record<string, unknown>;
  if (String(record.code ?? "").trim().toUpperCase() === normalizedCode) return true;
  return preferredObjectKeys.some((key) => errorHasCode(record[key], normalizedCode, seen));
}

function extractErrorCode(value: unknown, seen = new Set<object>()): string {
  if (typeof value === "string") {
    if (!/^\s*[{[]/.test(value)) return "";
    try {
      return extractErrorCode(JSON.parse(value), seen);
    } catch {
      return "";
    }
  }
  if (!value || typeof value !== "object" || seen.has(value)) return "";
  seen.add(value);
  const record = value as Record<string, unknown>;
  if (typeof record.code === "string") return record.code.trim();
  for (const key of preferredObjectKeys) {
    const code = extractErrorCode(record[key], seen);
    if (code) return code;
  }
  return "";
}

function extractText(value: unknown, seen = new Set<object>()): string {
  if (typeof value === "string") return value;
  if (value instanceof Error) return value.message;
  if (!value || typeof value !== "object") return "";
  if (seen.has(value)) return "";
  seen.add(value);

  const record = value as Record<string, unknown>;
  for (const key of preferredObjectKeys) {
    const text = extractText(record[key], seen);
    if (text.trim()) return text;
  }
  return "";
}

function firstReadableLine(value: string): string {
  const withoutAnsi = value.replace(/\u001b\[[0-9;]*m/g, "").trim();
  const withoutTrace = withoutAnsi
    .split(/\n\s*(?:Traceback\b|Stack(?: trace)?\s*:|Caused by\s*:|File\s+["']|at\s+\S)/i, 1)[0]
    .replace(/\s*(?:Traceback \(most recent call last\):|stack trace\s*:).*$/i, "")
    .trim();
  const line = withoutTrace
    .split(/\r?\n/)
    .map((item) => item.trim())
    .find(Boolean) ?? "";
  return line
    .replace(/^(?:Error|InvokeError|RuntimeError|Exception)\s*:\s*/i, "")
    .replace(/[：:]\s*$/, "")
    .replace(/\s+/g, " ")
    .trim();
}

function knownTencentApiError(value: string): string | null {
  const code = value.match(/["']?errcode["']?\s*:\s*["']?(-?\d+)/i)?.[1] ?? "";
  if (code === "40058" || /invalid request parameter/i.test(value)) {
    return `腾讯文档请求参数无效${code ? `（错误码 ${code}）` : ""}，请检查字段 Schema、字段映射和枚举选项。`;
  }
  return null;
}

function knownFriendlyMessage(value: string): string | null {
  const normalized = value.toLowerCase();
  if (/revision_required/i.test(value)) {
    return "配置版本缺失，请刷新页面后重新操作。";
  }
  if (/invalid_listener/i.test(value)) {
    return "监听器配置无效，请检查群聊、名称和时序设置。";
  }
  if (/runtime_unavailable|runtime_protocol_error|internal_error/i.test(value)) {
    return "群监听后台暂时不可用，请刷新后重试。";
  }
  if (/configuration revision changed|revision_conflict/i.test(value)) {
    return "配置已被其他页面更新，请刷新后重新操作。";
  }
  if (/only one enabled listener is allowed for a group|group_already_listened/i.test(value)) {
    return "这个群已经有一个启用中的监听器，请先停用原配置。";
  }
  if (/tool grant no longer matches discovered schema|invalid_tool_grant|tool_schema_changed/i.test(value)) {
    return "MCP 工具的 Schema 已变化，请重新测试服务并确认工具授权。";
  }
  if (/automatic sending requires a visible webhook test confirmation|webhook_confirmation_required/i.test(value)) {
    return "自动发送前，请先发送 webhook 测试并确认消息出现在所选群。";
  }
  if (/sender account\/mobile could not be resolved|true_mention_unavailable/i.test(value)) {
    return "无法解析企微真艾特，可明确选择普通文本 @姓名或放弃发送。";
  }
  if (/delivery result is unknown|delivery_unknown|delivery_confirmation_required/i.test(value)) {
    return "发送结果未知，请先到群里核实，系统不会自动重发。";
  }
  if (/work item changed|work_version_conflict/i.test(value)) {
    return "这条回复的状态已经变化，请刷新后重新操作。";
  }
  if (/official wecom group robot url|invalid_webhook_url/i.test(value)) {
    return "请填写官方企微群机器人 webhook 地址。";
  }
  const isModelService = /大模型|\b(?:llm|openai|model)\b/i.test(value);
  const isTencentService = /腾讯|smart\s*sheet|tencent/i.test(value);
  const tencentApiError = isTencentService ? knownTencentApiError(value) : null;
  if (tencentApiError) return tencentApiError;
  if (/大模型.*(?:没有\s*choices|有效\s*json|json\s*必须)/i.test(value)) {
    return "大模型返回格式不符合预期，请更换模型或调整提示词后重试。";
  }
  if (/无法连接大模型服务|大模型.*(?:network|connect)/i.test(value)) {
    return "无法连接大模型服务，请检查接口地址和网络后重试。";
  }
  if (/无法连接腾讯接口|腾讯.*(?:network|connect)/i.test(value)) {
    return "无法连接腾讯文档服务，请检查网络和腾讯相关配置后重试。";
  }
  if (/\b(?:401|unauthori[sz]ed|authentication failed|invalid api[ _-]?key)\b/.test(normalized)) {
    return "认证失败，请检查 API Key 或相关凭据。";
  }
  if (/\b(?:403|forbidden)\b/.test(normalized)) {
    return "当前凭据没有执行权限，请检查账号授权。";
  }
  if (/\b(?:429|rate limit|too many requests)\b/.test(normalized)) {
    return "请求过于频繁，请稍后再试。";
  }
  if (/\b(?:400|bad request)\b/.test(normalized)) {
    if (isModelService) return "模型服务不接受当前请求，请检查模型名称、接口地址和兼容性设置。";
    if (isTencentService) return "腾讯文档接口不接受当前请求，请检查 Smart Sheet 配置。";
    return "远程服务不接受当前请求，请检查相关配置。";
  }
  if (/\b(?:404|not found)\b/.test(normalized)) {
    if (isModelService) return "找不到模型服务或指定模型，请检查接口地址和模型名称。";
    if (isTencentService) return "找不到腾讯文档接口或目标表格，请检查 Smart Sheet 配置。";
    return "找不到远程服务或目标资源，请检查相关配置。";
  }
  if (/\b(?:5\d{2}|bad gateway|service unavailable)\b/.test(normalized)) {
    if (isModelService) return "模型服务暂时不可用，请稍后重试。";
    if (isTencentService) return "腾讯文档服务暂时不可用，请稍后重试。";
    return "远程服务暂时不可用，请稍后重试。";
  }
  if (/\b(?:timeout|timed out|etimedout)\b/.test(normalized)) {
    return "操作超时，请检查网络后重试。";
  }
  if (/\b(?:econnrefused|econnreset|enotfound|network error|failed to fetch|connection (?:failed|refused|reset))\b/.test(normalized)) {
    return "网络连接失败，请检查网络和接口地址后重试。";
  }
  if (/\b(?:permission denied|access denied|eacces|eperm)\b/.test(normalized)) {
    return "没有访问权限，请检查目录权限或改用其他位置。";
  }
  if (/\b(?:enoent|no such file or directory)\b/.test(normalized)) {
    return "找不到所需文件或目录，请检查相关路径。";
  }
  if (/\b(?:jsondecodeerror|syntaxerror|unexpected token)\b/.test(normalized)) {
    return "返回数据格式异常，请检查相关服务配置后重试。";
  }
  return null;
}

function looksTechnical(value: string): boolean {
  return /(?:\b(?:panic|stack|traceback|exception|exit code|spawn|serde|tokio|python|rustc?|winerror|errno)\b|\bhttp\s+\d{3}\b|\bat\s+\S+\s*\(|[A-Za-z]:\\[^\s]+\\[^\s]|[{}\[\]])/i.test(value);
}

/** Converts bridge/runtime failures into a short message safe to show in the UI. */
export function toUserErrorMessage(
  reason: unknown,
  fallback = DEFAULT_MESSAGE,
): string {
  const code = extractErrorCode(reason);
  let raw = extractText(reason);
  if (/^\s*[{[]/.test(raw)) {
    try {
      raw = extractText(JSON.parse(raw)) || raw;
    } catch {
      // The original text is handled below when it is not JSON.
    }
  }
  const known = knownFriendlyMessage(`${code} ${raw}`.trim());
  if (known) return known;
  if (!raw.trim()) return fallback;

  const readable = firstReadableLine(raw);
  if (!readable || looksTechnical(readable)) return fallback;

  // Backend messages are usually already written for users. Keep concise Chinese
  // text, but do not expose arbitrary English runtime output.
  if (!/[\u3400-\u9fff]/u.test(readable)) return fallback;
  if (readable.length <= MAX_MESSAGE_LENGTH) return readable;
  return `${readable.slice(0, MAX_MESSAGE_LENGTH - 1).trimEnd()}…`;
}

import { describe, expect, it } from "vitest";
import { toUserErrorMessage } from "./errors";

describe("toUserErrorMessage", () => {
  it("keeps a concise Chinese backend message and removes its stack", () => {
    const error = new Error("读取配置失败，请检查配置文件。\n    at bootstrap (src/App.tsx:20:3)");
    expect(toUserErrorMessage(error)).toBe("读取配置失败，请检查配置文件。");
  });

  it("removes an inline Python traceback", () => {
    const reason = "处理引擎执行失败：Traceback (most recent call last):\n  File \"worker/main.py\", line 10";
    expect(toUserErrorMessage(reason)).toBe("处理引擎执行失败");
  });

  it("turns authentication details into an actionable prompt", () => {
    const reason = { message: "Request failed: 401 Unauthorized\nTraceback: secret internals" };
    expect(toUserErrorMessage(reason)).toBe("认证失败，请检查 API Key 或相关凭据。");
  });

  it("maps common network and permission failures", () => {
    expect(toUserErrorMessage("ECONNREFUSED 127.0.0.1:3000")).toBe(
      "网络连接失败，请检查网络和接口地址后重试。",
    );
    expect(toUserErrorMessage("Permission denied: C:\\private\\chat.db")).toBe(
      "没有访问权限，请检查目录权限或改用其他位置。",
    );
  });

  it("hides model-service JSON responses and malformed model output", () => {
    expect(toUserErrorMessage(
      '大模型请求失败 HTTP 400: {"error":{"message":"invalid request payload","code":"bad_request"}}',
    )).toBe("模型服务不接受当前请求，请检查模型名称、接口地址和兼容性设置。");
    expect(toUserErrorMessage(
      '大模型响应中没有 choices: {"request_id":"secret-runtime-detail"}',
    )).toBe("大模型返回格式不符合预期，请更换模型或调整提示词后重试。");
    expect(toUserErrorMessage(
      '腾讯接口 HTTP 500: {"trace_id":"secret-runtime-detail"}',
    )).toBe("腾讯文档服务暂时不可用，请稍后重试。");
  });

  it("keeps an actionable Tencent error when a gateway wraps error 40058", () => {
    const expected = "腾讯文档请求参数无效（错误码 40058），请检查字段 Schema、字段映射和枚举选项。";
    expect(toUserErrorMessage(
      '腾讯接口 HTTP 500: {"errcode":40058,"errmsg":"invalid Request Parameter, hint: [secret-hint]"}',
    )).toBe(expected);
    expect(toUserErrorMessage(
      '腾讯文档写入失败: {"errcode":40058,"errmsg":"invalid Request Parameter, hint: [secret-hint]"}',
    )).toBe(expected);
  });

  it("reads a message from a serialized bridge error", () => {
    expect(toUserErrorMessage('{"message":"配置文件格式不正确。","stack":"hidden"}')).toBe(
      "配置文件格式不正确。",
    );
  });

  it("uses the caller fallback for stack-only and cyclic objects", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.error = cyclic;
    expect(toUserErrorMessage("Error: at worker.run (worker.rs:42)", "启动失败，请重试。")).toBe(
      "启动失败，请重试。",
    );
    expect(toUserErrorMessage(cyclic, "保存失败，请重试。")).toBe("保存失败，请重试。");
  });

  it("limits unexpectedly long user-facing messages", () => {
    const message = `读取失败：${"内容".repeat(100)}`;
    const result = toUserErrorMessage(message);
    expect(result.endsWith("…")).toBe(true);
    expect(result.length).toBeLessThanOrEqual(120);
  });

  it("turns ReplyRuntime protocol failures into operator actions", () => {
    expect(toUserErrorMessage("configuration revision changed from 3 to 4")).toBe(
      "配置已被其他页面更新，请刷新后重新操作。",
    );
    expect(toUserErrorMessage("only one enabled listener is allowed for a group")).toBe(
      "这个群已经有一个启用中的监听器，请先停用原配置。",
    );
    expect(toUserErrorMessage("tool grant no longer matches discovered schema: kb/search")).toBe(
      "MCP 工具的 Schema 已变化，请重新测试服务并确认工具授权。",
    );
    expect(toUserErrorMessage("delivery result is unknown; confirm the message is absent before retrying")).toBe(
      "发送结果未知，请先到群里核实，系统不会自动重发。",
    );
  });
});

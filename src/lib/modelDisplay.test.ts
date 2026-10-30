import { describe, expect, it } from "vitest";

import { modelCapabilityDetail, summarizeModelCapability } from "./modelDisplay";
import type { ModelCapabilityStatus, ModelConfig } from "../types";

const llm = (overrides: Partial<ModelConfig> = {}): ModelConfig => ({
  provider: "openai_compatible",
  base_url: "https://api.example.com/v1",
  api_key: "sk-test",
  model: "deepseek-v4.1-flash",
  ...overrides,
});

const capability = (overrides: Partial<ModelCapabilityStatus> = {}): ModelCapabilityStatus => ({
  revision: 3,
  available: true,
  current: true,
  fingerprint: "fp-new",
  testedAt: "2026-10-30T10:00:00+08:00",
  result: {
    status: "success",
    provider: "openai_compatible",
    model: "deepseek-v4.1-flash",
    capabilities: {},
  },
  ...overrides,
});

describe("model capability display", () => {
  it("labels the current model when the probe matches", () => {
    const summary = summarizeModelCapability(capability(), llm());
    expect(summary.currentModel).toBe("deepseek-v4.1-flash");
    expect(summary.isCurrent).toBe(true);
    expect(summary.staleModel).toBe("");
    expect(summary.headline).toBe("检测结果与当前模型配置一致");
  });

  it("never shows the previous model as the current value after a config change", () => {
    const status = capability({
      current: false,
      fingerprint: "fp-old",
      result: {
        status: "success",
        provider: "openai_compatible",
        model: "qwen3.7-max",
        capabilities: {},
      },
    });
    const summary = summarizeModelCapability(status, llm());
    expect(summary.currentModel).toBe("deepseek-v4.1-flash");
    expect(summary.staleModel).toBe("qwen3.7-max");
    expect(summary.isCurrent).toBe(false);
    expect(summary.headline).toBe("模型配置已变化，请重新检测");
    const detail = modelCapabilityDetail(summary);
    expect(detail).toContain("deepseek-v4.1-flash");
    expect(detail).toContain("上次检测：qwen3.7-max（已过期）");
    expect(detail).not.toContain("openai_compatible · qwen3.7-max");
  });

  it("handles the never-tested and unconfigured states", () => {
    expect(summarizeModelCapability(null, llm()).headline).toBe("尚未检测当前模型");
    const summary = summarizeModelCapability(null, llm({ model: "", provider: "" }));
    expect(summary.currentModel).toBe("未配置模型");
    expect(summary.currentProvider).toBe("openai_compatible");
  });
});

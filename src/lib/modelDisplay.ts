/**
 * Display helpers for the global model configuration.
 *
 * A saved model is the only truth. A capability probe result is history: once
 * the configuration fingerprint changes, the old model name must not be shown
 * as if it were the current one.
 */

import type { ModelCapabilityStatus, ModelConfig } from "../types";

export interface ModelCapabilitySummary {
  /** Model currently saved in the global settings; always the primary label. */
  currentModel: string;
  currentProvider: string;
  /** Last tested model name when it differs from the current configuration. */
  staleModel: string;
  headline: string;
  isCurrent: boolean;
  fingerprint: string;
  testedAt: string;
}

export function summarizeModelCapability(
  status: ModelCapabilityStatus | null | undefined,
  llm: ModelConfig,
): ModelCapabilitySummary {
  const currentModel = (llm.model || "").trim() || "未配置模型";
  const currentProvider = (llm.provider || "").trim() || "openai_compatible";
  const testedAt = status?.testedAt || "";
  const isCurrent = Boolean(status?.current) && testedAt !== "";
  const testedModel = String(status?.result?.model || "").trim();
  const staleModel =
    !isCurrent && testedAt && testedModel && testedModel !== currentModel ? testedModel : "";
  const headline = isCurrent
    ? "检测结果与当前模型配置一致"
    : testedAt
      ? "模型配置已变化，请重新检测"
      : "尚未检测当前模型";
  return {
    currentModel,
    currentProvider,
    staleModel,
    headline,
    isCurrent,
    fingerprint: status?.fingerprint || "",
    testedAt,
  };
}

/** Secondary line: current provider/model plus an optional stale-history note. */
export function modelCapabilityDetail(summary: ModelCapabilitySummary): string {
  const parts = [`${summary.currentProvider} · ${summary.currentModel}`];
  if (summary.fingerprint) parts.push(`配置指纹 ${summary.fingerprint}`);
  if (summary.staleModel) parts.push(`上次检测：${summary.staleModel}（已过期）`);
  return parts.join(" · ");
}

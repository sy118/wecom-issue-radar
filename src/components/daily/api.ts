/**
 * Typed wrappers around `bridge.dailyAction` / `bridge.wechatFillInput`.
 *
 * The renderer also runs against older preload builds, so a missing bridge
 * method becomes a readable Chinese error instead of a TypeError.
 */

import { bridge } from "../../lib/bridge";
import {
  eqReplyPayload,
  normalizePhrase,
  normalizePhrases,
  summarizePayload,
} from "./logic";
import type {
  DigestMode,
  EqReplyResult,
  Phrase,
  PhraseDraft,
  SummarizeResult,
  WechatFillResult,
} from "./types";

export const MISSING_DAILY_SERVICE = "当前版本还没有沟通助手服务，请更新应用后再试。";
export const MISSING_WECHAT_FILL = "当前版本不支持填入微信，请先复制文案后手动粘贴。";

type Payload = Record<string, unknown>;

interface DailyBridgeSurface {
  dailyAction?<T>(action: string, payload?: Payload): Promise<T>;
  wechatFillInput?(chatTitle: string, text: string): Promise<WechatFillResult>;
}

const surface = bridge as unknown as DailyBridgeSurface;

function dailyAction<T>(action: string, payload: Payload = {}): Promise<T> {
  if (typeof surface.dailyAction !== "function") {
    return Promise.reject(new Error(MISSING_DAILY_SERVICE));
  }
  return surface.dailyAction<T>(action, payload);
}

export function generateEqReply(input: {
  text: string;
  context?: string;
  recipient?: string;
}): Promise<EqReplyResult> {
  return dailyAction<EqReplyResult>("eq_reply", eqReplyPayload(input));
}

export function summarizeText(text: string, mode: DigestMode): Promise<SummarizeResult> {
  return dailyAction<SummarizeResult>("summarize", summarizePayload(text, mode));
}

export async function listPhrases(): Promise<Phrase[]> {
  const result = await dailyAction<unknown>("phrases_list");
  return normalizePhrases(result);
}

export async function savePhrase(draft: PhraseDraft): Promise<Phrase> {
  const payload: Payload = {
    phrase: {
      title: draft.title.trim(),
      category: draft.category.trim() || "未分类",
      content: draft.content.trim(),
    },
  };
  if (draft.id) (payload.phrase as Record<string, unknown>).id = draft.id;
  const result = await dailyAction<unknown>("phrase_save", payload);
  const phrase = normalizePhrase(
    result && typeof result === "object" ? (result as Record<string, unknown>).phrase : result,
  );
  if (!phrase) throw new Error("话术保存成功，但返回内容无法识别，请刷新后重试。");
  return phrase;
}

export async function deletePhrase(id: string): Promise<void> {
  await dailyAction("phrase_delete", { id });
}

export function supportsWechatFill(): boolean {
  return typeof surface.wechatFillInput === "function";
}

export async function fillWechat(chatTitle: string, text: string): Promise<WechatFillResult> {
  if (typeof surface.wechatFillInput !== "function") {
    throw new Error(MISSING_WECHAT_FILL);
  }
  return surface.wechatFillInput(chatTitle, text);
}

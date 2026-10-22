/**
 * Wire shapes for the v4.2 沟通助手 actions (`bridge.dailyAction`).
 *
 * Backend contract (task-21):
 *   eq_reply        {text, context?, recipient?} -> {styles, interpretation, check, fallback}
 *   summarize       {text, mode:"digest"|"meeting"} -> {summary, points, risks, next, fallback}
 *   phrases_list    {} -> {phrases}
 *   phrase_save     {phrase:{id?,title,category,content,variables?}} -> {phrase}
 *   phrase_delete   {id} -> {ok:true}
 *
 * Every field below is optional-tolerant: the renderer normalizes responses in
 * `logic.ts` before they reach the UI, so a partially filled payload never crashes a page.
 */

export type EqStyleId = "steady" | "warm" | "brief";

export interface EqStyle {
  id: EqStyleId | string;
  title: string;
  content: string;
}

export interface EqSignal {
  label: string;
  detail: string;
}

export interface EqInterpretation {
  summary: string;
  signals: EqSignal[];
}

export interface EqCheck {
  risks: string[];
  suggestions: string[];
}

export interface EqReplyResult {
  styles: EqStyle[];
  interpretation: EqInterpretation;
  check: EqCheck;
  fallback: boolean;
}

export type DigestMode = "digest" | "meeting";

export interface SummarizeResult {
  summary: string;
  points: string[];
  risks: string[];
  next: string[];
  fallback: boolean;
}

export interface Phrase {
  id: string;
  title: string;
  category: string;
  content: string;
  variables: string[];
}

export interface PhrasesListResult {
  phrases: Phrase[];
}

export interface PhraseSaveResult {
  phrase: Phrase;
}

export interface WechatFillResult {
  filled: boolean;
  chatTitle: string;
  characters: number;
}

/** Editable phrase draft; `id` is absent until the backend assigns one. */
export interface PhraseDraft {
  id?: string;
  title: string;
  category: string;
  content: string;
}

/** Editable digest fields; arrays are edited as one line per item. */
export interface DigestDrafts {
  summary: string;
  points: string;
  risks: string;
  next: string;
}

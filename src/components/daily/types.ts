/** Wire shapes for the M3 daily assistant actions (`searchAction` / `dailyAction`). */

export type DailySource = "wecom" | "wechat" | string;

export interface SearchStatus {
  indexed: number;
  total: number;
  building: boolean;
  progress: number;
  lastIndexedAt?: string | null;
  error?: string | null;
}

export interface SearchResultItem {
  id: string;
  source: DailySource;
  convId: string;
  convName: string;
  kind: string;
  senderName: string;
  text: string;
  ts: number | string;
  msgId?: string;
}

export interface SearchQueryResult {
  items: SearchResultItem[];
  total: number;
}

export interface SearchContextMessage {
  id?: string;
  msgId?: string;
  source?: DailySource;
  senderName?: string;
  kind?: string;
  text?: string;
  ts?: number | string;
  [key: string]: unknown;
}

export interface SearchContextResult {
  messages: SearchContextMessage[];
}

export type EqStyleId = "steady" | "warm" | "brief";

export interface EqStyle {
  id: EqStyleId | string;
  title: string;
  content: string;
}

export interface EqReplyResult {
  styles: EqStyle[];
  fallback: boolean;
}

export interface WeeklyStats {
  [key: string]: unknown;
}

export interface WeeklyResult {
  draft: string;
  stats: WeeklyStats;
  fallback: boolean;
}

export interface TodoItem {
  id: string;
  title?: string;
  text?: string;
  done?: boolean;
  source?: DailySource;
  convName?: string;
  dueAt?: string;
  createdAt?: string;
  [key: string]: unknown;
}

export interface TodoListResult {
  items: TodoItem[];
}

export interface TodoRefreshResult {
  created: number;
  items: TodoItem[];
}

export interface TodoUpdateResult {
  item: TodoItem;
}

export interface WechatFillResult {
  filled: boolean;
  chatTitle: string;
  characters: number;
}

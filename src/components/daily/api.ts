import { bridge } from "../../lib/bridge";
import type {
  DailySource,
  EqReplyResult,
  SearchContextResult,
  SearchQueryResult,
  SearchStatus,
  TodoItem,
  TodoListResult,
  TodoRefreshResult,
  TodoUpdateResult,
  WechatFillResult,
  WeeklyResult,
} from "./types";

/**
 * The M3 daily-assistant commands are provided by the preload bridge. The
 * renderer build is also used before the bridge exports them, so the calls are
 * typed here and fail with a user-facing message instead of a TypeError.
 */
type Payload = Record<string, unknown>;

interface DailyBridgeSurface {
  searchAction?<T>(action: string, payload?: Payload): Promise<T>;
  dailyAction?<T>(action: string, payload?: Payload): Promise<T>;
  wechatFillInput?(chatTitle: string, text: string): Promise<WechatFillResult>;
}

const surface = bridge as unknown as DailyBridgeSurface;

const MISSING_SEARCH = "当前版本还没有聊天搜索服务，请更新应用后再试。";
const MISSING_DAILY = "当前版本还没有日常助手服务，请更新应用后再试。";
const MISSING_FILL = "当前版本不支持填入微信，请先复制文案后手动粘贴。";

export async function searchAction<T>(action: string, payload?: Payload): Promise<T> {
  const fn = surface.searchAction;
  if (typeof fn !== "function") throw new Error(MISSING_SEARCH);
  return fn<T>(action, payload);
}

export async function dailyAction<T>(action: string, payload?: Payload): Promise<T> {
  const fn = surface.dailyAction;
  if (typeof fn !== "function") throw new Error(MISSING_DAILY);
  return fn<T>(action, payload);
}

export async function searchStatus(): Promise<SearchStatus> {
  return searchAction<SearchStatus>("status");
}

export async function searchQuery(payload: Payload): Promise<SearchQueryResult> {
  return searchAction<SearchQueryResult>("query", payload);
}

export async function searchContext(id: string, radius = 3): Promise<SearchContextResult> {
  return searchAction<SearchContextResult>("context", { id, radius });
}

export async function rebuildSearchIndex(): Promise<{ started: boolean }> {
  return searchAction<{ started: boolean }>("rebuild");
}

export async function generateEqReply(text: string, context?: string): Promise<EqReplyResult> {
  const payload: Payload = { text };
  const extra = context?.trim();
  if (extra) payload.context = extra;
  return dailyAction<EqReplyResult>("eq_reply", payload);
}

export async function generateWeeklyReport(
  startDate: string,
  endDate: string,
  sources?: DailySource[],
): Promise<WeeklyResult> {
  const payload: Payload = { startDate, endDate };
  if (sources?.length) payload.sources = [...sources];
  return dailyAction<WeeklyResult>("weekly", payload);
}

export async function listTodos(status?: "open" | "done"): Promise<TodoListResult> {
  const payload: Payload = {};
  if (status) payload.status = status;
  return dailyAction<TodoListResult>("todos", payload);
}

export async function refreshTodos(): Promise<TodoRefreshResult> {
  return dailyAction<TodoRefreshResult>("todos_refresh");
}

export async function updateTodo(id: string, done: boolean): Promise<TodoItem> {
  const result = await dailyAction<TodoUpdateResult>("todo_update", { id, done });
  return result.item;
}

export function supportsWechatFill(): boolean {
  return typeof surface.wechatFillInput === "function";
}

export async function fillWechat(chatTitle: string, text: string): Promise<WechatFillResult> {
  const fn = surface.wechatFillInput;
  if (typeof fn !== "function") throw new Error(MISSING_FILL);
  return fn.call(surface, chatTitle, text);
}

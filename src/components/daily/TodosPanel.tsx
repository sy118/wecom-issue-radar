import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Inbox, LoaderCircle, RefreshCw, RotateCcw, SkipForward } from "lucide-react";
import { toast } from "sonner";

import { toUserErrorMessage } from "../../lib/errors";
import { Button } from "../ui";
import { listTodos, refreshTodos, updateTodo } from "./api";
import { formatMessageTime, sourceLabel } from "./logic";
import type { TodoItem } from "./types";
import { DailyEmpty, DailyError, DailyLoading } from "./ui";

type TodoFilter = "open" | "done" | "all";

const FILTERS: Array<{ id: TodoFilter; label: string }> = [
  { id: "open", label: "待完成" },
  { id: "done", label: "已完成" },
  { id: "all", label: "全部" },
];

function todoTitle(item: TodoItem): string {
  return item.title?.trim() || item.text?.trim() || "未命名待办";
}

export function TodosPanel() {
  const [filter, setFilter] = useState<TodoFilter>("open");
  const [items, setItems] = useState<TodoItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async (nextFilter: TodoFilter) => {
    setLoading(true);
    setError("");
    try {
      const result = await listTodos(nextFilter === "all" ? undefined : nextFilter);
      setItems(Array.isArray(result.items) ? result.items : []);
    } catch (reason) {
      setError(toUserErrorMessage(reason, "无法读取待办列表，请稍后重试。"));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(filter);
  }, [filter, load]);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const result = await refreshTodos();
      setItems(Array.isArray(result.items) ? result.items : []);
      const created = Number(result.created ?? 0);
      if (created > 0) toast.success(`新增 ${created} 条待办`);
      else toast.success("没有新的待办");
    } catch (reason) {
      toast.error("刷新待办失败", {
        description: toUserErrorMessage(reason, "请稍后重试。"),
      });
    } finally {
      setRefreshing(false);
    }
  };

  const handleToggle = async (item: TodoItem) => {
    setBusyId(item.id);
    try {
      const updated = await updateTodo(item.id, item.done !== true);
      setItems((current) => {
        const next = current.map((entry) => (entry.id === updated.id ? { ...entry, ...updated } : entry));
        return filter === "open" && updated.done === true
          ? next.filter((entry) => entry.id !== updated.id)
          : next;
      });
    } catch (reason) {
      toast.error("更新待办失败", {
        description: toUserErrorMessage(reason, "请稍后重试。"),
      });
    } finally {
      setBusyId("");
    }
  };

  const handleSkip = async (item: TodoItem) => {
    setBusyId(item.id);
    try {
      const updated = await updateTodo(item.id, true);
      setItems((current) => {
        const next = current.map((entry) => (entry.id === updated.id ? { ...entry, ...updated } : entry));
        return filter === "open" ? next.filter((entry) => entry.id !== updated.id) : next;
      });
      toast.success("已跳过该待办");
    } catch (reason) {
      toast.error("跳过待办失败", {
        description: toUserErrorMessage(reason, "请稍后重试。"),
      });
    } finally {
      setBusyId("");
    }
  };

  return (
    <div className="daily-workspace">
      <div className="daily-todos-toolbar">
        <div className="daily-tabs" role="tablist" aria-label="待办筛选">
          {FILTERS.map((entry) => (
            <button
              key={entry.id}
              type="button"
              role="tab"
              aria-selected={filter === entry.id}
              onClick={() => setFilter(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </div>
        <div className="daily-actions">
          <Button variant="secondary" onClick={() => void handleRefresh()} disabled={refreshing}>
            <RefreshCw className={refreshing ? "spin" : undefined} size={15} />
            {refreshing ? "刷新中…" : "从聊天里找待办"}
          </Button>
        </div>
      </div>

      {error ? (
        <DailyError message={error} onRetry={() => void load(filter)} />
      ) : loading ? (
        <DailyLoading label="正在读取待办…" />
      ) : items.length === 0 ? (
        <DailyEmpty
          icon={<Inbox size={28} />}
          title={filter === "done" ? "还没有已完成的待办" : "暂时没有待办"}
          body="待办来自聊天里的明确承诺或请求；点「从聊天里找待办」可以让 AI 重新扫描最近的会话。"
        />
      ) : (
        <div className="daily-todos">
          {items.map((item) => {
            const done = item.done === true;
            const time = formatMessageTime(item.dueAt ?? item.createdAt);
            return (
              <article className={done ? "daily-todo is-done" : "daily-todo"} key={item.id}>
                <label className="daily-checkbox" title={done ? "标记为未完成" : "标记为已完成"}>
                  <input
                    type="checkbox"
                    checked={done}
                    disabled={busyId === item.id}
                    aria-label={`${done ? "标记未完成" : "标记完成"}：${todoTitle(item)}`}
                    onChange={() => void handleToggle(item)}
                  />
                </label>
                <div className="daily-todo-main">
                  <strong className={done ? "is-done" : undefined}>{todoTitle(item)}</strong>
                  <div className="daily-todo-meta">
                    {item.source && <span>{sourceLabel(item.source)}</span>}
                    {item.convName && <span>{item.convName}</span>}
                    {item.dueAt && <span>截止 {formatMessageTime(item.dueAt)}</span>}
                  </div>
                </div>
                <div className="daily-todo-side">
                  <span className="daily-todo-date">
                    {busyId === item.id ? <LoaderCircle className="spin" size={15} /> : done ? <CheckCircle2 size={16} /> : time}
                  </span>
                  {done ? (
                    <Button variant="ghost" title="恢复为未完成" aria-label="恢复为未完成" onClick={() => void handleToggle(item)}>
                      <RotateCcw size={15} />
                    </Button>
                  ) : (
                    <Button variant="ghost" title="跳过这个待办" aria-label="跳过这个待办" onClick={() => void handleSkip(item)}>
                      <SkipForward size={15} />
                    </Button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

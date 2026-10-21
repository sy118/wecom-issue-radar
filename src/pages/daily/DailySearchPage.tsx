import { useCallback, useEffect, useState } from "react";
import {
  Database,
  Inbox,
  LoaderCircle,
  RefreshCw,
  Search,
} from "lucide-react";
import { toast } from "sonner";

import { Button, Field, Input } from "../../components/ui";
import { toUserErrorMessage } from "../../lib/errors";
import { searchContext, searchQuery, searchStatus, rebuildSearchIndex } from "../../components/daily/api";
import { buildSearchPayload, formatMessageTime, kindLabel, progressPercent, type SearchFilters } from "../../components/daily/logic";
import type { SearchContextMessage, SearchResultItem, SearchStatus } from "../../components/daily/types";
import {
  DailyEmpty,
  DailyError,
  DailyLoading,
  DailyNote,
  DailyPageHeader,
  HighlightedText,
  SourceBadge,
} from "../../components/daily/ui";

const PAGE_SIZE = 30;
const EMPTY_FILTERS: SearchFilters = {
  query: "",
  source: "all",
  sender: "",
  from: "",
  to: "",
  kinds: [],
};

const SOURCE_OPTIONS = [
  { value: "all", label: "全部来源" },
  { value: "wecom", label: "企业微信" },
  { value: "wechat", label: "个人微信" },
];

const KIND_OPTIONS = [
  { value: "text", label: "文本" },
  { value: "image", label: "图片" },
  { value: "file", label: "文件" },
  { value: "voice", label: "语音" },
  { value: "link", label: "链接" },
];

export function DailySearchPage() {
  const [filters, setFilters] = useState<SearchFilters>(EMPTY_FILTERS);
  const [status, setStatus] = useState<SearchStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [items, setItems] = useState<SearchResultItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [searched, setSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState("");
  const [contexts, setContexts] = useState<Record<string, SearchContextMessage[]>>({});
  const [contextLoading, setContextLoading] = useState("");

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await searchStatus());
      setStatusError("");
    } catch (reason) {
      setStatusError(toUserErrorMessage(reason, "无法读取索引状态，请确认后台服务已启动。"));
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    if (!status?.building) return;
    const timer = window.setInterval(() => void loadStatus(), 1500);
    return () => window.clearInterval(timer);
  }, [status?.building, loadStatus]);

  const updateFilter = <K extends keyof SearchFilters,>(key: K, value: SearchFilters[K]) => {
    setFilters((current) => ({ ...current, [key]: value }));
  };

  const toggleKind = (kind: string) => {
    setFilters((current) => ({
      ...current,
      kinds: current.kinds.includes(kind)
        ? current.kinds.filter((entry) => entry !== kind)
        : [...current.kinds, kind],
    }));
  };

  const runSearch = async (nextPage: number) => {
    setSearching(true);
    setError("");
    try {
      const result = await searchQuery(buildSearchPayload(filters, PAGE_SIZE, nextPage * PAGE_SIZE));
      setItems(Array.isArray(result.items) ? result.items : []);
      setTotal(Number(result.total ?? 0));
      setPage(nextPage);
      setSearched(true);
      setExpanded("");
    } catch (reason) {
      setError(toUserErrorMessage(reason, "搜索失败，请稍后重试。"));
      setItems([]);
      setTotal(0);
    } finally {
      setSearching(false);
    }
  };

  const handleRebuild = async () => {
    setRebuilding(true);
    try {
      const result = await rebuildSearchIndex();
      toast.success(result.started ? "已开始重建索引" : "索引重建已在进行中");
      await loadStatus();
    } catch (reason) {
      toast.error("重建索引失败", {
        description: toUserErrorMessage(reason, "请确认后台服务已启动后重试。"),
      });
    } finally {
      setRebuilding(false);
    }
  };

  const toggleContext = async (item: SearchResultItem) => {
    if (expanded === item.id) {
      setExpanded("");
      return;
    }
    setExpanded(item.id);
    if (contexts[item.id]) return;
    setContextLoading(item.id);
    try {
      const result = await searchContext(item.id, 3);
      setContexts((current) => ({ ...current, [item.id]: result.messages ?? [] }));
    } catch (reason) {
      toast.error("无法读取上下文", {
        description: toUserErrorMessage(reason, "请稍后重试。"),
      });
      setExpanded("");
    } finally {
      setContextLoading("");
    }
  };

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const indexed = (status?.indexed ?? 0) > 0;

  return (
    <div className="page-content daily-page">
      <DailyPageHeader
        eyebrow="Daily Search"
        icon={<Search size={13} />}
        title="聊天查找"
        description="在本地索引里搜索企业微信群聊和个人微信会话；内容不会上传。"
        actions={
          <Button variant="secondary" onClick={() => void loadStatus()}>
            <RefreshCw size={15} />刷新状态
          </Button>
        }
      />

      <section className="glass-card">
        <div className="daily-status">
          <div className="daily-status-main">
            <strong>
              {status === null
                ? "正在读取索引状态…"
                : status.building
                  ? "正在建立索引"
                  : indexed
                    ? `已索引 ${status.indexed ?? 0} 条消息`
                    : "还没有建立索引"}
            </strong>
            <span>共 {status?.total ?? 0} 条可选消息</span>
            {status?.lastIndexedAt && <span>上次索引：{formatMessageTime(status.lastIndexedAt)}</span>}
            {status?.error && <span>索引异常：{status.error}</span>}
          </div>
          <div className="daily-actions">
            {status?.building && (
              <div className="daily-progress" aria-label={`索引进度 ${progressPercent(status.progress)}%`}>
                <i style={{ width: `${progressPercent(status.progress)}%` }} />
              </div>
            )}
            <Button variant="secondary" onClick={() => void handleRebuild()} disabled={rebuilding || Boolean(status?.building)}>
              {rebuilding || status?.building ? <LoaderCircle className="spin" size={15} /> : <Database size={15} />}
              {status?.building ? "索引中…" : "重建索引"}
            </Button>
          </div>
        </div>
        {statusError && <div style={{ marginTop: "var(--space-3)" }}><DailyError message={statusError} onRetry={() => void loadStatus()} /></div>}
      </section>

      <section className="glass-card">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void runSearch(0);
          }}
        >
          <div className="daily-toolbar">
            <Field label="关键词">
              <Input
                value={filters.query}
                onChange={(event) => updateFilter("query", event.target.value)}
                placeholder="例如：退款、登录失败、客户名称"
                autoComplete="off"
              />
            </Field>
            <Field label="来源">
              <select className="input" value={filters.source} onChange={(event) => updateFilter("source", event.target.value)}>
                {SOURCE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="发送者">
              <Input
                value={filters.sender}
                onChange={(event) => updateFilter("sender", event.target.value)}
                placeholder="昵称或备注"
              />
            </Field>
            <div className="daily-toolbar-actions">
              <Button type="submit" disabled={searching}>
                {searching ? <LoaderCircle className="spin" size={15} /> : <Search size={15} />}
                {searching ? "搜索中…" : "搜索"}
              </Button>
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  setFilters(EMPTY_FILTERS);
                  setItems([]);
                  setTotal(0);
                  setSearched(false);
                  setError("");
                  setExpanded("");
                }}
              >
                重置
              </Button>
            </div>
          </div>

          <div className="daily-filter-row">
            <Field label="开始日期">
              <Input type="date" value={filters.from} onChange={(event) => updateFilter("from", event.target.value)} />
            </Field>
            <Field label="结束日期">
              <Input type="date" value={filters.to} onChange={(event) => updateFilter("to", event.target.value)} />
            </Field>
            <div className="field">
              <span className="field-label">消息类型</span>
              <div className="daily-check-row" style={{ marginTop: 0 }}>
                {KIND_OPTIONS.map((option) => (
                  <label className="daily-check" key={option.value}>
                    <input
                      type="checkbox"
                      checked={filters.kinds.includes(option.value)}
                      onChange={() => toggleKind(option.value)}
                    />
                    {option.label}
                  </label>
                ))}
              </div>
            </div>          </div>
        </form>

        <div style={{ marginTop: "var(--space-3)" }}>
          <DailyNote>搜索只读取本机索引；不改动原始聊天记录，也不会上传任何内容。</DailyNote>
        </div>
      </section>

      <section className="glass-card">
        {searching ? (
          <DailyLoading label="正在搜索…" />
        ) : error ? (
          <DailyError message={error} onRetry={() => void runSearch(page)} />
        ) : status === null ? (
          <DailyLoading label="正在读取索引状态…" />
        ) : !indexed && !status.building ? (
          <DailyEmpty
            icon={<Database size={28} />}
            title="还没有建立聊天索引"
            body="第一次使用需要先扫描本机聊天记录建立索引；之后才能按关键词、发送者和时间搜索。"
            action={
              <Button onClick={() => void handleRebuild()} disabled={rebuilding}>
                {rebuilding ? <LoaderCircle className="spin" size={15} /> : <Database size={15} />}
                建立索引
              </Button>
            }
          />
        ) : searched && items.length === 0 ? (
          <DailyEmpty
            icon={<Inbox size={28} />}
            title="没有找到匹配的消息"
            body="换个关键词，或放宽发送者、时间和来源筛选后再试。"
          />
        ) : items.length === 0 ? (
          <DailyEmpty
            icon={<Search size={28} />}
            title="输入关键词开始查找"
            body="可以只按发送者或时间范围筛选；结果会显示来源、会话和时间，点击可展开上下文。"
          />
        ) : (
          <>
            <div className="daily-results">
              {items.map((item) => {
                const open = expanded === item.id;
                const context = contexts[item.id] ?? [];
                return (
                  <article className="daily-result-card" key={item.id}>
                    <button
                      type="button"
                      className="daily-result"
                      aria-expanded={open}
                      onClick={() => void toggleContext(item)}
                    >
                      <div className="daily-result-head">
                        <SourceBadge source={item.source} />
                        <span>{item.convName || "未知会话"}</span>
                        <span>·</span>
                        <span>{item.senderName || "未知发送者"}</span>
                        <span>·</span>
                        <time>{formatMessageTime(item.ts)}</time>
                        <span className="daily-badge daily-badge-muted">{kindLabel(item.kind)}</span>
                      </div>
                      <p className="daily-result-text">
                        <HighlightedText text={item.text} query={filters.query} />
                      </p>
                    </button>
                    {open && (
                      <div className="daily-context">
                        {contextLoading === item.id ? (
                          <DailyLoading label="正在读取上下文…" />
                        ) : context.length === 0 ? (
                          <p className="daily-hint">这条消息附近没有可显示的上下文。</p>
                        ) : (
                          context.map((message, index) => {
                            const target =
                              (message.id !== undefined && message.id === item.id) ||
                              (message.msgId !== undefined && item.msgId !== undefined && message.msgId === item.msgId);
                            return (
                              <div className={target ? "daily-context-row is-target" : "daily-context-row"} key={`${message.id ?? message.msgId ?? index}-${index}`}>
                                <span className="daily-context-meta">
                                  {formatMessageTime(message.ts)}
                                  {message.senderName ? ` · ${message.senderName}` : ""}
                                </span>
                                <p className="daily-context-text">
                                  <HighlightedText text={message.text ?? ""} query={filters.query} />
                                </p>
                              </div>
                            );
                          })
                        )}
                      </div>
                    )}
                  </article>
                );
              })}
            </div>

            <div className="daily-pagination">
              <span>
                第 {page + 1} / {pageCount} 页 · 共 {total} 条
              </span>
              <div className="daily-actions">
                <Button variant="secondary" disabled={page <= 0 || searching} onClick={() => void runSearch(page - 1)}>
                  上一页
                </Button>
                <Button variant="secondary" disabled={page + 1 >= pageCount || searching} onClick={() => void runSearch(page + 1)}>
                  下一页
                </Button>
              </div>
            </div>
          </>
        )}
      </section>
    </div>
  );
}

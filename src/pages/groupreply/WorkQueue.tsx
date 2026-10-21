/**
 * Work queue views (active / pending / history) including the filter bar.
 * The three lists are rendered in the same order and with the same markup as
 * the original page so existing DOM contracts are preserved.
 */

import { Activity, History, Inbox, Search, X } from "lucide-react";

import type { ReplyWorkItem } from "../../types";
import { WorkCard, WorkPagination } from "./common";
import {
  WORK_PAGE_SIZE,
  type WorkFilters,
  type WorkPageState,
} from "./shared";

export interface WorkQueueProps {
  view: "active" | "pending" | "history";
  filters: WorkFilters;
  onFiltersChange: (filters: WorkFilters, resetPages: boolean) => void;
  activeItems: ReplyWorkItem[];
  pendingItems: ReplyWorkItem[];
  historyItems: ReplyWorkItem[];
  activePage: WorkPageState;
  pendingPage: WorkPageState;
  historyPage: WorkPageState;
  onActivePageChange: (page: number) => void;
  onPendingPageChange: (page: number) => void;
  onHistoryPageChange: (page: number) => void;
  onOpenWork: (item: ReplyWorkItem) => void;
}

export function WorkQueue({
  view,
  filters,
  onFiltersChange,
  activeItems,
  pendingItems,
  historyItems,
  activePage,
  pendingPage,
  historyPage,
  onActivePageChange,
  onPendingPageChange,
  onHistoryPageChange,
  onOpenWork,
}: WorkQueueProps) {
  return (
    <>
      <div className="work-filter-bar" aria-label="工作项筛选">
        <label className="work-filter-search"><Search size={14} /><input value={filters.search} placeholder="搜索问题、发送者或群 ID" onChange={(event) => onFiltersChange({ ...filters, search: event.target.value }, true)} /></label>
        <select aria-label="运行时版本" value={filters.runtimeVersion} onChange={(event) => onFiltersChange({ ...filters, runtimeVersion: event.target.value as WorkFilters["runtimeVersion"] }, false)}><option value="">全部 Runtime</option><option value="v2">Agent v2</option><option value="legacy">Legacy</option></select>
        <select aria-label="回答引擎" value={filters.answerEngine} onChange={(event) => onFiltersChange({ ...filters, answerEngine: event.target.value as WorkFilters["answerEngine"] }, false)}><option value="">全部引擎</option><option value="mcp">MCP Agent</option><option value="dify">Dify</option></select>
        <select aria-label="处理阶段" value={filters.stage} onChange={(event) => onFiltersChange({ ...filters, stage: event.target.value }, false)}><option value="">全部阶段</option><option value="collecting">收集补充</option><option value="waiting_for_image">等待缓存</option><option value="waiting_for_human_reply">等待群友</option><option value="retrieving">检索中</option><option value="pending">待发送</option><option value="sent">已发送</option><option value="failed">失败</option><option value="skipped_no_evidence">无证据</option><option value="skipped_review_failed">复核拒绝</option></select>
        <select aria-label="停止原因" value={filters.stopReason} onChange={(event) => onFiltersChange({ ...filters, stopReason: event.target.value }, false)}><option value="">全部停止原因</option><option value="model_stopped">模型主动停止</option><option value="max_rounds">达到轮次上限</option><option value="max_tool_calls">达到调用上限</option><option value="timeout">运行超时</option><option value="no_tools">无可用工具</option></select>
        <select aria-label="恢复状态" value={filters.recovery} onChange={(event) => onFiltersChange({ ...filters, recovery: event.target.value as WorkFilters["recovery"] }, false)}><option value="">全部恢复状态</option><option value="recovered">恢复/重试</option><option value="normal">普通执行</option></select>
        {Object.values(filters).some(Boolean) && <button type="button" className="work-filter-reset" onClick={() => onFiltersChange({ search: "", runtimeVersion: "", answerEngine: "", stage: "", stopReason: "", recovery: "" }, false)}><X size={13} />清除</button>}
      </div>

      {view === "active" && (
        activeItems.length ? <><div className="work-list">{activeItems.map((item) => <WorkCard key={item.id} item={item} onOpen={() => onOpenWork(item)} />)}</div><WorkPagination value={activePage} onChange={onActivePageChange} /></>
          : <div className="runtime-empty runtime-empty-framed"><Activity size={29} /><strong>当前没有处理中问题</strong><span>发现新问题后，可在这里查看补充收集、群友等待和回答引擎检索的实时阶段。</span></div>
      )}

      {view === "pending" && (
        pendingItems.length ? <><div className="work-list">{pendingItems.map((item) => <WorkCard key={item.id} item={item} onOpen={() => onOpenWork(item)} />)}</div><WorkPagination value={pendingPage} onChange={onPendingPageChange} /></>
          : <div className="runtime-empty runtime-empty-framed"><Inbox size={29} /><strong>没有待发送回复</strong><span>审核模式下通过证据检查的回答，会在这里等待发送或放弃。</span></div>
      )}

      {view === "history" && (
        historyItems.length ? <><div className="work-list">{historyItems.map((item) => <WorkCard key={item.id} item={item} onOpen={() => onOpenWork(item)} />)}</div><WorkPagination value={historyPage} onChange={onHistoryPageChange} /></>
          : <div className="runtime-empty runtime-empty-framed"><History size={29} /><strong>暂无处理历史</strong><span>非问题、群友已回答、证据不足、发送成功和失败都会留下可审计结果。</span></div>
      )}
    </>
  );
}

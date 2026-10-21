/** Execution history modal for scheduled tasks. */

import {
  AlertCircle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleMinus,
  FolderOpen,
  History,
  LoaderCircle,
  RefreshCw,
  UsersRound,
  XCircle,
} from "lucide-react";

import { Button } from "../../components/ui";
import { toUserErrorMessage } from "../../lib/errors";
import type {
  ScheduleDefinition,
  ScheduleExecutionHistoryPage,
} from "../../types";
import {
  executionHistoryCounts,
  executionHistoryRunDetail,
  executionHistoryStatus,
  executionHistoryStatusLabel,
  historyDateLabel,
} from "./shared";

export interface ExecutionHistoryModalProps {
  sortedSchedules: ScheduleDefinition[];
  historyScheduleId: string;
  historyPage: number;
  historyLoading: boolean;
  historyData: ScheduleExecutionHistoryPage | null;
  historyError: string;
  onClose: () => void;
  onFilter: (scheduleId: string) => void;
  onRefresh: () => void;
  onChangePage: (page: number) => void;
  onOpenResult: (path: string) => void;
}

export function ExecutionHistoryModal({
  sortedSchedules,
  historyScheduleId,
  historyPage,
  historyLoading,
  historyData,
  historyError,
  onClose,
  onFilter,
  onRefresh,
  onChangePage,
  onOpenResult,
}: ExecutionHistoryModalProps) {
  return (
    <div className="modal-backdrop schedule-modal-backdrop">
      <div className="schedule-modal schedule-history-modal" role="dialog" aria-modal="true" aria-labelledby="schedule-history-title">
        <div className="schedule-modal-header">
          <div>
            <span className="eyebrow">Execution history</span>
            <h2 id="schedule-history-title">定时任务执行记录</h2>
          </div>
          <button type="button" aria-label="关闭执行记录" onClick={onClose}>×</button>
        </div>
        <div className="schedule-history-toolbar">
          <label>
            <span>筛选任务</span>
            <select
              className="input"
              value={historyScheduleId}
              onChange={(event) => onFilter(event.target.value)}
            >
              <option value="">全部定时任务</option>
              {sortedSchedules.map((schedule) => (
                <option key={schedule.id} value={schedule.id}>{schedule.name}</option>
              ))}
            </select>
          </label>
          <Button variant="ghost" disabled={historyLoading} onClick={onRefresh}>
            <RefreshCw size={14} className={historyLoading ? "spin" : undefined} />刷新
          </Button>
        </div>
        <div className="schedule-modal-body schedule-history-body">
          {historyLoading && !historyData ? (
            <div className="schedule-history-state">
              <LoaderCircle size={25} className="spin" />
              <strong>正在读取执行记录…</strong>
              <span>记录保存在本机，不会触发新的任务。</span>
            </div>
          ) : historyError ? (
            <div className="schedule-history-state schedule-history-error">
              <AlertCircle size={25} />
              <strong>执行记录读取失败</strong>
              <span>{historyError}</span>
              <Button variant="secondary" onClick={onRefresh}>重新加载</Button>
            </div>
          ) : historyData && historyData.items.length === 0 ? (
            <div className="schedule-history-state">
              <History size={27} />
              <strong>{historyScheduleId ? "该任务还没有执行记录" : "还没有执行记录"}</strong>
              <span>任务完成后，结果会自动保存在这里。</span>
            </div>
          ) : (
            <div className={historyLoading ? "schedule-history-list is-loading" : "schedule-history-list"}>
              {historyData?.items.map((item) => {
                const status = executionHistoryStatus(item);
                const statusLabel = executionHistoryStatusLabel(item);
                const counts = executionHistoryCounts(item.result);
                const runs = item.result?.runs ?? [];
                const itemMessage = toUserErrorMessage(
                  item.message,
                  item.success ? "任务执行完成" : "任务执行失败，请检查配置后重试。",
                );
                const hasDetailedCounts = runs.some((run) => Boolean(run.status))
                  || item.result?.successCount !== undefined
                  || item.result?.emptyCount !== undefined
                  || item.result?.failedCount !== undefined;
                return (
                  <article className="schedule-history-item" key={item.executionId}>
                    <div className={`schedule-history-status history-status-${status}`}>
                      {status === "success" && <CheckCircle2 size={18} />}
                      {status === "partial" && <AlertCircle size={18} />}
                      {status === "empty" && <CircleMinus size={18} />}
                      {status === "failed" && <XCircle size={18} />}
                    </div>
                    <div className="schedule-history-main">
                      <div className="schedule-history-heading">
                        <div>
                          <strong>{item.scheduleName || item.scheduleId}</strong>
                          <span className={`history-status-badge history-status-${status}`}>{statusLabel}</span>
                        </div>
                        <time dateTime={item.finishedAt}>{historyDateLabel(item.finishedAt)}</time>
                      </div>
                      <div className="schedule-history-meta">
                        <span>{item.trigger === "manual" ? "手动执行" : "自动执行"}</span>
                        {item.startedAt && <span>开始于 {historyDateLabel(item.startedAt)}</span>}
                        {hasDetailedCounts && <span>成功 {counts.success} · 无记录 {counts.empty} · 失败 {counts.failed}</span>}
                      </div>
                      <p>{itemMessage}</p>
                      {runs.length > 0 && (
                        <details className="schedule-history-details">
                          <summary><UsersRound size={13} />查看 {runs.length} 个群的处理结果</summary>
                          <div className="schedule-history-groups">
                            {runs.map((run, index) => {
                              const runStatus = run.status ?? "success";
                              const outputCount = Object.keys(run.outputs ?? {}).length;
                              const runDetail = executionHistoryRunDetail(run);
                              return (
                                <div className={`schedule-history-group history-group-${runStatus}`} key={`${run.groupId}-${index}`}>
                                  <span className="schedule-history-group-icon">
                                    {runStatus === "success" && <CheckCircle2 size={14} />}
                                    {runStatus === "empty" && <CircleMinus size={14} />}
                                    {runStatus === "failed" && <XCircle size={14} />}
                                  </span>
                                  <span className="schedule-history-group-main">
                                    <strong>{run.groupName || run.groupId}</strong>
                                    <small>{runDetail}</small>
                                  </span>
                                  {run.dayDir && (runStatus === "success" || outputCount > 0) && (
                                    <button type="button" title="打开本地结果目录" onClick={() => onOpenResult(run.dayDir)}>
                                      <FolderOpen size={14} />
                                    </button>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        </details>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </div>
        <div className="schedule-modal-footer schedule-history-footer">
          <span>
            共 {historyData?.total ?? 0} 次
            {historyData && historyData.totalPages > 0
              ? ` · 第 ${historyData.page} / ${historyData.totalPages} 页`
              : ""}
          </span>
          <Button
            variant="secondary"
            disabled={historyLoading || !historyData || historyData.page <= 1}
            onClick={() => onChangePage((historyData?.page ?? historyPage) - 1)}
          >
            <ChevronLeft size={14} />上一页
          </Button>
          <Button
            variant="secondary"
            disabled={historyLoading || !historyData || historyData.page >= historyData.totalPages}
            onClick={() => onChangePage((historyData?.page ?? historyPage) + 1)}
          >
            下一页<ChevronRight size={14} />
          </Button>
        </div>
      </div>
    </div>
  );
}

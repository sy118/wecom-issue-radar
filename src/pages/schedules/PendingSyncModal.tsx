/** Confirmation modal for writing pending scheduled results to Tencent Docs. */

import { CloudUpload, LoaderCircle, Table2 } from "lucide-react";

import { Button } from "../../components/ui";
import type { TaskRunResult } from "../../types";
import type { frozenTargetsFrom } from "./shared";

export interface PendingSyncModalProps {
  confirmingScheduleName: string;
  confirmingRuns: TaskRunResult[];
  confirmingPendingCount: number;
  confirmingTargets: ReturnType<typeof frozenTargetsFrom>;
  confirmingWarnings: string[];
  confirmingBlockers: string[];
  confirmationRefreshError: string;
  refreshingConfirmation: boolean;
  syncing: boolean;
  refreshedConfirmingRuns: TaskRunResult[] | null;
  onDiscard: () => void;
  onClose: () => void;
  onConfirm: () => void;
}

export function PendingSyncModal({
  confirmingScheduleName,
  confirmingRuns,
  confirmingPendingCount,
  confirmingTargets,
  confirmingWarnings,
  confirmingBlockers,
  confirmationRefreshError,
  refreshingConfirmation,
  syncing,
  refreshedConfirmingRuns,
  onDiscard,
  onClose,
  onConfirm,
}: PendingSyncModalProps) {
  return (
    <div className="modal-backdrop">
      <div className="modal-card schedule-sync-modal smart-sheet-sync-modal">
        <div className="modal-icon"><CloudUpload size={24} /></div>
        <h2>确认写入腾讯文档？</h2>
        {refreshingConfirmation ? (
          <p><LoaderCircle className="spin" size={15} /> 正在按当前配置刷新每个群的腾讯文档预览…</p>
        ) : confirmationRefreshError ? (
          <p>定时任务“{confirmingScheduleName}”的当前预览刷新失败，本次确认已被阻止。</p>
        ) : (
          <p>
            定时任务“{confirmingScheduleName}”的 <strong>{confirmingRuns.length}</strong> 个群共有{" "}
            <strong>{confirmingPendingCount}</strong> 条新问题待写入。所有群同步成功后，待确认结果才会清除。
          </p>
        )}
        {!refreshingConfirmation && !confirmationRefreshError && (
          <div className="sync-template-list">
            {confirmingTargets.map((target) => (
              <div key={target.key}>
                <span><Table2 size={14} /></span>
                <span>
                  <strong>{target.name} · {target.pending} 条</strong>
                  <small>{target.groups.join("、")}</small>
                  <small>{target.url || "未提供腾讯文档模板 URL"}</small>
                </span>
              </div>
            ))}
          </div>
        )}
        {confirmationRefreshError && (
          <div className="sync-blockers"><span>{confirmationRefreshError}</span></div>
        )}
        {confirmingWarnings.length > 0 && (
          <div className="sync-blockers schedule-sync-warnings">
            {confirmingWarnings.map((message) => <span key={message}>{message}</span>)}
            <small>以上是确认前刷新得到的当前状态；尝试写入时 Worker 仍会再次校验。</small>
          </div>
        )}
        {confirmingBlockers.length > 0 && (
          <div className="sync-blockers">
            {confirmingBlockers.map((message) => <span key={message}>{message}</span>)}
          </div>
        )}
        <div className="modal-note">
          {refreshingConfirmation
            ? "刷新预览只读取当前本地结果和模板配置，不会向腾讯文档写入数据。"
            : confirmationRefreshError
              ? "待确认结果已保留。修复模板配置后关闭弹窗并重新打开，即可再次刷新。"
              : confirmingWarnings.length
                ? "当前字段映射或 Webhook 配置无效，请先到设置中修复并保存，再重新打开确认窗口。"
              : confirmingBlockers.length
            ? "冻结结果缺少同步所需信息，无法自动写入；可放弃此待确认结果后重新执行任务。"
            : "这是外部写入操作。中断后重试前请先核对目标文档；仅已取得远端记录 ID 且成功落入本地台账的记录会跳过。"}
        </div>
        <div className="modal-actions schedule-sync-modal-actions">
          <Button variant="danger" disabled={syncing || refreshingConfirmation} onClick={onDiscard}>
            放弃待确认
          </Button>
          <span />
          <Button variant="secondary" disabled={syncing} onClick={onClose}>取消</Button>
          <Button
            disabled={
              syncing
              || refreshingConfirmation
              || refreshedConfirmingRuns === null
              || Boolean(confirmationRefreshError)
              || confirmingWarnings.length > 0
              || confirmingBlockers.length > 0
            }
            onClick={onConfirm}
          >
            {syncing && <LoaderCircle className="spin" size={16} />}
            {confirmingPendingCount > 0 ? `确认写入 ${confirmingPendingCount} 条` : "确认并清除"}
          </Button>
        </div>
      </div>
    </div>
  );
}

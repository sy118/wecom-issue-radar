/** Small presentational pieces shared by the group reply sub-views. */

import {
  Activity,
  CheckCircle2,
  ChevronRight,
  History,
  Inbox,
  LoaderCircle,
  XCircle,
} from "lucide-react";

import type { ReplyWorkItem } from "../../types";
import {
  WORK_PAGE_SIZE,
  dateLabel,
  workAnswerCopy,
  workStatusCopy,
  type WorkPageState,
} from "./shared";

export function RuntimeLoading({ label }: { label: string }) {
  return <div className="runtime-empty"><LoaderCircle className="spin" /><strong>{label}</strong><span>正在同步本机运行状态。</span></div>;
}

export function WorkPagination({ value, onChange }: { value: WorkPageState; onChange: (page: number) => void }) {
  const totalPages = Math.max(1, Math.ceil(value.total / WORK_PAGE_SIZE));
  if (!value.total) return null;
  return (
    <nav className="work-pagination" aria-label="回复记录分页">
      <span>第 <strong>{value.page}</strong> / {totalPages} 页 · 共 {value.total} 条</span>
      <div>
        <button type="button" disabled={value.page <= 1} onClick={() => onChange(value.page - 1)}>上一页</button>
        <button type="button" disabled={value.page >= totalPages} onClick={() => onChange(value.page + 1)}>下一页</button>
      </div>
    </nav>
  );
}

export function WorkCard({ item, onOpen }: { item: ReplyWorkItem; onOpen: () => void }) {
  const status = workStatusCopy(item);
  const mediaSummary = (item.imageCount ?? 0) > 0 || (item.fileCount ?? 0) > 0
    ? `${item.imageCount ?? 0} 图 · ${item.fileCount ?? 0} 文件`
    : "";
  return <button className="work-card" onClick={onOpen}><div className={`work-card-status status-${item.status}`}>{item.status === "pending" ? <Inbox size={15} /> : item.status === "sent" ? <CheckCircle2 size={15} /> : item.status === "failed" ? <XCircle size={15} /> : <Activity size={15} />}</div><div className="work-card-main"><div><strong>{item.senderName}</strong><span>{item.groupName}</span><em className="work-engine-badge">{item.answerEngine === "dify" ? "DIFY" : "MCP"}</em>{mediaSummary && <em className="work-media-badge">{mediaSummary}</em>}{(item.duplicateCount ?? 0) > 0 && <em className="work-duplicate-badge"><History size={10} />已折叠 {item.duplicateCount}</em>}</div><p>{item.question || "问题内容不可用"}</p><small>{workAnswerCopy(item)}</small></div><div className="work-card-side"><span>{status}</span><time>{dateLabel(item.updatedAt || item.createdAt)}</time><ChevronRight size={13} /></div></button>;
}

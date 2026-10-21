/** Work item detail modal (timeline, evidence and review/send actions). */

import {
  Activity,
  AlertTriangle,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleOff,
  Clock3,
  Eye,
  FileUp,
  History,
  LoaderCircle,
  MessageSquareReply,
  RefreshCw,
  Send,
  ShieldAlert,
  Wrench,
  X,
  XCircle,
} from "lucide-react";

import { Button } from "../../components/ui";
import type { WorkActionKind } from "../../lib/replyRuntimeUi";
import type { ReplyWorkItem } from "../../types";
import {
  dateLabel,
  imageStatusCopy,
  retryableWorkStages,
  timelineGroups,
  timelineKindCopy,
  timelineRoleCopy,
  workAnswerCopy,
  workStageStepCopy,
  workStageTimeline,
} from "./shared";

export interface WorkDetailDrawerProps {
  detail: ReplyWorkItem;
  busy: string;
  onClose: () => void;
  workAction: (item: ReplyWorkItem, kind: WorkActionKind) => void;
  onExportDiagnostics: (item: ReplyWorkItem) => void;
}

export function WorkDetailDrawer({
  detail,
  busy,
  onClose,
  workAction,
  onExportDiagnostics,
}: WorkDetailDrawerProps) {
  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
      <section className="modal-card work-detail-modal" role="dialog" aria-modal="true">
        <button className="work-detail-close" aria-label="关闭" onClick={onClose}><X size={15} /></button>
        <div className="work-detail-kicker"><MessageSquareReply size={14} />{detail.groupName} · {detail.senderName} · {detail.answerEngine === "dify" ? "Dify Chatflow" : "MCP Agent"}</div>
        <h2>{detail.stage === "needs_image" ? "需要图片" : detail.status === "pending" ? "待发送回复" : "处理详情"}</h2>
        {(detail.imageCount ?? 0) > 0 && <div className={`work-outcome-banner is-${detail.imageStatus ?? "none"}`}>
          {detail.stage === "needs_image" || ["partial", "unavailable", "unsupported"].includes(detail.imageStatus ?? "")
            ? <AlertTriangle size={18} />
            : detail.imageStatus === "resolving" ? <LoaderCircle className="spin" size={18} /> : <CheckCircle2 size={18} />}
          <span>
            <strong>{detail.stage === "needs_image" ? "图片未参与分析，系统已停止自动回复" : "图片处理结果"}</strong>
            <small>{imageStatusCopy(detail.imageStatus, detail.imageCount, detail.imageAvailableCount, detail.imageUnavailableCount)}</small>
          </span>
        </div>}
        <div className="work-stage-timeline" aria-label="处理阶段">
          {workStageTimeline(detail).map((step) => (
            <div className={`work-stage-step is-${step.state}`} key={step.key}>
              <span className="work-stage-marker">
                {step.state === "complete" ? <Check size={11} />
                  : step.state === "current" ? <LoaderCircle className="spin" size={11} />
                    : step.state === "failed" ? <X size={11} />
                    : step.state === "skipped" ? <CircleOff size={11} /> : <span />}
              </span>
              <span><strong>{step.label}</strong><small>{workStageStepCopy(step)}</small></span>
            </div>
          ))}
        </div>
        <div className="work-detail-context">
          {detail.sourceDelaySeconds !== undefined && <div className="image-state"><Clock3 size={13} /><span><strong>来源观测延迟</strong><small>消息发送后约 {detail.sourceDelaySeconds.toFixed(1)} 秒被本地监听发现（含企微写库与轮询）</small></span></div>}
          <div className={`image-state is-${detail.imageStatus ?? "none"}`}><Eye size={13} /><span><strong>图片读取</strong><small>{imageStatusCopy(detail.imageStatus, detail.imageCount, detail.imageAvailableCount, detail.imageUnavailableCount)}</small></span></div>
          {(detail.fileCount ?? 0) > 0 && <div className={(detail.fileUnavailableCount ?? 0) > 0 ? "image-state is-partial" : "image-state is-ready"}><FileUp size={13} /><span><strong>普通文件读取</strong><small>{detail.fileAvailableCount ?? 0}/{detail.fileCount ?? 0} 个文件可用{(detail.fileUnavailableCount ?? 0) > 0 ? `，${detail.fileUnavailableCount} 个不可用` : ""}</small></span></div>}
          {(detail.duplicateCount ?? 0) > 0 && <div className="duplicate-state"><History size={13} /><span><strong>重复记录已合并</strong><small>已折叠 {detail.duplicateCount} 条重复记录</small></span></div>}
        </div>
        <div className="work-detail-section"><span>识别到的问题</span><p>{detail.question || "问题内容不可用"}</p></div>
        <div className="work-detail-section answer"><span>{detail.answerEngine === "dify" ? "基于 Dify 原始回答与检索资源的复核结果" : "基于 MCP 工具证据的回答"}</span><p>{workAnswerCopy(detail)}</p></div>
        <div className="work-agent-summary" aria-label="Agent 执行摘要"><span><strong>{detail.runtimeVersion === "v2" ? "Agent v2" : "Legacy"}</strong><small>Runtime</small></span><span><strong>{Number(detail.providerAudit?.rounds ?? 0)}</strong><small>决策轮次</small></span><span><strong>{Number(detail.providerAudit?.toolCalls ?? 0)}</strong><small>工具尝试</small></span><span><strong>{detail.stopReason || String(detail.providerAudit?.stopReason ?? "—")}</strong><small>停止原因</small></span><span><strong>{Number(detail.usage?.totalTokens ?? ((detail.providerAudit?.usage as Record<string, unknown> | undefined)?.totalTokens) ?? 0)}</strong><small>Token</small></span></div>
        {detail.progress && (detail.status === "waiting" || detail.status === "working") && <div className="work-live-progress" aria-label="实时进度"><span><LoaderCircle className="spin" size={14} /><strong>{detail.progress.stageLabel || "正在执行"}</strong><small>第 {detail.progress.currentRound ?? 0}/{detail.progress.maxRounds ?? 6} 轮 · {detail.progress.toolCalls ?? 0} 次工具调用</small></span><em>{detail.progress.lastEventKind ? `最近事件：${timelineKindCopy[detail.progress.lastEventKind] ?? detail.progress.lastEventKind}` : ""}{detail.progress.elapsedSeconds !== undefined ? ` · ${detail.progress.elapsedSeconds}s` : ""}</em></div>}
        {(detail.timeline?.length ?? 0) > 0 ? <section className="agent-timeline-panel"><div className="agent-timeline-heading"><span><Activity size={14} /><strong>Agent 执行时间线</strong></span><small>{detail.timeline?.length} 个事件 · 内容明细受诊断开关控制</small></div>{timelineGroups(detail).map(([role, events]) => <details className="agent-role-group" key={role} open={role === "retriever" || role === "reviewer"}><summary><span>{timelineRoleCopy[role] ?? role}</span><em>{events.length}</em><ChevronDown size={14} /></summary><div>{events.map((event) => {
          const metadata = event.metadata ?? {};
          const failed = event.kind.includes("failed") || event.kind.includes("error") || event.kind.includes("invalid");
          return <article className={`agent-event-row ${failed ? "is-failed" : ""}`} key={`${event.workGeneration}-${event.sequence}`}><span className="agent-event-marker">{failed ? <XCircle size={12} /> : event.kind.includes("started") ? <LoaderCircle size={12} /> : <Check size={12} />}</span><div><header><strong>{timelineKindCopy[event.kind] ?? event.kind}</strong><time>{dateLabel(event.createdAt)}</time></header><p>{metadata.serverId || metadata.toolName ? `${String(metadata.serverId ?? "MCP")} / ${String(metadata.toolName ?? "工具")}` : `角色：${timelineRoleCopy[event.role] ?? event.role}`}</p><div className="agent-event-badges">{metadata.round !== undefined && <span>第 {String(metadata.round)} 轮</span>}{metadata.durationMs !== undefined && <span>{String(metadata.durationMs)} ms</span>}{metadata.evidenceCount !== undefined && <span>证据 {String(metadata.evidenceCount)}</span>}{event.kind.includes("cache_hit") && <span>缓存命中</span>}{metadata.resumed === true && <span>恢复执行</span>}{metadata.usableEvidence === false && <span>空结果</span>}</div>{event.detail ? <details className="agent-event-detail"><summary>查看诊断明细</summary><pre>{JSON.stringify(event.detail, null, 2)}</pre></details> : <small className="agent-detail-off">详细诊断未开启或该事件没有正文</small>}</div></article>;
        })}</div></details>)}</section> : <div className="agent-timeline-empty"><Activity size={15} /><span><strong>暂无 Agent 时间线</strong><small>Legacy 历史任务或检索前结束的任务可能没有事件记录。</small></span></div>}
        {detail.evidence?.length ? <details className="work-evidence"><summary>{detail.answerEngine === "dify" ? <Bot size={14} /> : <Wrench size={14} />}检索证据（{detail.evidence.length}）<ChevronDown size={14} /></summary>{detail.evidence.map((entry, index) => <div key={`${entry.provider}-${entry.toolName}-${index}`}>{entry.provider === "dify" ? <Bot size={13} /> : <Wrench size={13} />}<span><strong>{entry.provider === "dify" ? "Dify Chatflow" : `${entry.serverName || "MCP"} / ${entry.toolName || "工具"}`}</strong><small>{entry.summary}</small></span></div>)}</details> : null}
        {detail.stage === "delivery_unknown" && <div className="runtime-safety-note is-danger"><ShieldAlert size={14} /><span><strong>发送结果未知</strong>系统不会自动重发。请先到群里核实；只有确认消息确实未出现后，才能明确选择重新发送。</span></div>}
        {detail.stage === "needs_image" ? <div className="work-detail-actions is-image-actions">
          <Button onClick={() => workAction(detail, "work.retry_images")} disabled={busy.includes(detail.id)}><RefreshCw size={14} />重新读取图片</Button>
          <Button variant="secondary" onClick={() => workAction(detail, "work.continue_without_images")} disabled={busy.includes(detail.id)}><Eye size={14} />使用现有内容继续</Button>
          <Button variant="danger" onClick={() => workAction(detail, "work.discard")} disabled={busy.includes(detail.id)}><XCircle size={14} />放弃任务</Button>
        </div> : detail.status === "pending" && <div className="work-detail-actions">
          {detail.stage === "delivery_unknown"
            ? (detail.mentionMode !== "unresolved"
                ? <Button variant="secondary" onClick={() => workAction(detail, "work.send")} disabled={busy.includes(detail.id)}><Send size={13} />确认群内未出现，重新发送</Button>
                : <Button variant="secondary" onClick={() => workAction(detail, "work.send_plain_at")} disabled={busy.includes(detail.id)}><AlertTriangle size={13} />确认未出现，普通 @ 重发</Button>)
            : (detail.mentionMode !== "unresolved" ? <Button onClick={() => workAction(detail, "work.send")} disabled={busy.includes(detail.id)}><Send size={13} />发送并真艾特</Button> : <Button variant="secondary" onClick={() => workAction(detail, "work.send_plain_at")} disabled={busy.includes(detail.id)}><AlertTriangle size={13} />普通 @姓名</Button>)}
          <Button variant="danger" onClick={() => workAction(detail, "work.discard")} disabled={busy.includes(detail.id)}><XCircle size={13} />放弃发送</Button>
        </div>}
        <div className="work-detail-sticky-actions"><Button variant="secondary" onClick={() => onExportDiagnostics(detail)} disabled={busy.includes(detail.id)}><FileUp size={13} />导出诊断</Button>{retryableWorkStages.has(detail.stage ?? "") && <Button onClick={() => workAction(detail, "work.retry")} disabled={busy.includes(detail.id)}><RefreshCw size={13} />重新检索</Button>}</div>
      </section>
    </div>
  );
}

/** Listener card grid (listeners tab of the group reply page). */

import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Edit3,
  Eye,
  MessageCircleQuestion,
  Plus,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Trash2,
  UsersRound,
  Wrench,
} from "lucide-react";

import { Button } from "../../components/ui";
import type { DifyAppSummary, ReplyListenerSummary } from "../../types";
import { RuntimeLoading } from "./common";
import { dateLabel, healthCopy } from "./shared";

export interface ListenerListProps {
  loading: boolean;
  listeners: ReplyListenerSummary[];
  difyApps: DifyAppSummary[];
  onCreate: () => void;
  onEdit: (listener: ReplyListenerSummary) => void;
  onRemove: (listener: ReplyListenerSummary) => void;
}

export function ListenerList({
  loading,
  listeners,
  difyApps,
  onCreate,
  onEdit,
  onRemove,
}: ListenerListProps) {
  const difyAppNames = new Map(difyApps.map((app) => [app.id, app.name]));
  if (loading && !listeners.length) return <RuntimeLoading label="正在读取监听器" />;
  if (!listeners.length) {
    return (
      <div className="runtime-empty runtime-empty-framed"><MessageCircleQuestion size={30} /><strong>还没有群消息监听</strong><span>首个监听器默认只生成待审核回复，不会自动向群里发送。</span><Button onClick={onCreate}><Plus size={14} />创建监听器</Button></div>
    );
  }
  return (
    <div className="listener-grid">
      {listeners.map((listener) => {
        const health = healthCopy(listener.health);
        return (
          <article className="listener-card" key={listener.id}>
            <header>
              <div className="runtime-icon"><UsersRound size={16} /></div>
              <div><h3>{listener.name}</h3><p>{listener.groupName}</p></div>
              <span className={`runtime-status runtime-status-${health.tone}`}>{health.label}</span>
            </header>
            <div className="listener-badge-row"><span className="listener-runtime-badge is-v2">AGENT V2</span><span className="listener-engine-badge">{listener.answerEngine === "dify" ? "DIFY CHATFLOW" : "MCP AGENT"}</span>{(listener.recoverableCount ?? 0) > 0 && <span className="listener-recovery-badge"><RefreshCw size={11} />{listener.recoverableCount} 个恢复任务</span>}</div>
            <div className="listener-flowline" aria-label="处理流程">
              <span><MessageCircleQuestion size={11} />只认问题</span><ChevronRight size={10} />
              <span><Clock3 size={11} />补充 {listener.tuning.sameSenderMergeSeconds}s</span><ChevronRight size={10} />
              <span><Clock3 size={11} />等 {listener.tuning.humanReplyWaitSeconds}s</span><ChevronRight size={10} />
              <span>{listener.answerEngine === "dify" ? <Bot size={11} /> : <Wrench size={11} />}{listener.answerEngine === "dify" ? "Dify 回答" : "MCP 证据"}</span><ChevronRight size={10} />
              <span><ShieldCheck size={11} />独立审核</span>
            </div>
            <div className="listener-facts listener-facts-wide">
              <div>
                <strong className="listener-fact-value">
                  {listener.answerEngine === "dify"
                    ? difyAppNames.get(listener.difyAppId) ?? "未找到"
                    : <>{listener.tools.length}<em>个</em></>}
                </strong>
                <span className="listener-fact-label">{listener.answerEngine === "dify" ? "Chatflow 应用" : "精确授权工具"}</span>
              </div>
              <div>
                <strong className="listener-fact-value">
                  {listener.answerEngine === "dify"
                    ? "—"
                    : <>{listener.toolSafety?.readOnly ?? 0}<em>/</em>{listener.toolSafety?.blocked ?? 0}</>}
                </strong>
                <span className="listener-fact-label">{listener.answerEngine === "dify" ? "由 Dify 管理工具" : "只读 / 已阻止"}</span>
              </div>
              <div>
                <strong className="listener-fact-value">{listener.tuning.maxConcurrency}<em>路</em></strong>
                <span className="listener-fact-label">同时检索问题</span>
              </div>
              <div>
                <strong className="listener-fact-value">
                  {listener.answerEngine === "dify"
                    ? <>{Math.round(listener.tuning.difyTimeoutSeconds / 60)}<em>分</em></>
                    : <>{listener.tuning.maxAgentRounds}<em>轮</em><i>/</i>{listener.tuning.maxToolCalls}<em>次</em></>}
                </strong>
                <span className="listener-fact-label">{listener.answerEngine === "dify" ? "Dify 最长等待" : "轮次 / 调用预算"}</span>
              </div>
            </div>
            <div className="listener-delivery">
              <span className={listener.webhookVerified ? "is-safe" : ""}>{listener.webhookVerified ? <CheckCircle2 size={12} /> : <ShieldAlert size={12} />}{listener.webhookVerified ? "Webhook 群归属已确认" : listener.webhookConfigured ? "Webhook 待群内确认" : "Webhook 未配置"}</span>
              <span>{listener.deliveryMode === "automatic" ? <><Sparkles size={12} />自动发送</> : <><Eye size={12} />审核后发送</>}</span>
            </div>
            {(listener.health === "degraded" || listener.health === "error") && <p className="listener-health-warning"><AlertTriangle size={12} />{listener.healthMessage}</p>}
            <footer>
              <span>{listener.lastRun ? `最近：${listener.lastRun.status}${listener.lastRun.stopReason ? ` · ${listener.lastRun.stopReason}` : ""} · ${dateLabel(listener.lastRun.updatedAt)}` : listener.enabled ? "监听已启用 · 尚无运行记录" : "配置已停用"}</span>
              <div>
                <button title="编辑" aria-label={`编辑监听器：${listener.name}`} onClick={() => onEdit(listener)}><Edit3 size={14} /></button>
                <button className="danger-icon" title="删除" aria-label={`删除监听器：${listener.name}`} onClick={() => onRemove(listener)}><Trash2 size={14} /></button>
              </div>
            </footer>
          </article>
        );
      })}
    </div>
  );
}

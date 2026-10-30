/**
 * Listener editor drawer (new/edit group listener). All state and persistence
 * live in the page; this component only renders the form and forwards edits.
 *
 * P1: Agent V2 is the only runtime and MCP tools are granted automatically by
 * the backend, so the editor has four steps and no per-tool policy UI.
 */

import {
  AlertTriangle,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleOff,
  Eye,
  Gauge,
  LoaderCircle,
  Save,
  Search,
  Send,
  ShieldAlert,
  Sparkles,
  UsersRound,
  Webhook,
  Wrench,
} from "lucide-react";
import { toast } from "sonner";

import { Button, Field, Input, Switch } from "../../components/ui";
import { Overlay } from "../../components/Overlay";
import { DEFAULT_INDEPENDENT_REVIEW_PROMPT } from "../../lib/replyRuntimeUi";
import type { DifyAppSummary, GroupInfo } from "../../types";
import type { ListenerDraft, TestChallenge } from "./shared";

export interface ListenerEditorDrawerProps {
  draft: ListenerDraft;
  busy: string;
  onDraftChange: (draft: ListenerDraft) => void;
  onClose: () => void;
  filteredGroups: GroupInfo[];
  groupSearch: string;
  onGroupSearchChange: (value: string) => void;
  onClearTestChallenge: () => void;
  difyApps: DifyAppSummary[];
  selectedDifyApp?: DifyAppSummary;
  advancedOpen: boolean;
  onAdvancedOpenChange: (updater: (value: boolean) => boolean) => void;
  testChallenge: TestChallenge | null;
  testRequiresSave: boolean;
  saveBlockers: string[];
  editorIssueCount: number;
  onSave: () => void;
  onTestWebhook: () => void;
  onConfirmWebhook: () => void;
}

export function ListenerEditorDrawer(props: ListenerEditorDrawerProps) {
  const {
    draft,
    busy,
    onDraftChange,
    onClose,
    filteredGroups,
    groupSearch,
    onGroupSearchChange,
    onClearTestChallenge,
    difyApps,
    selectedDifyApp,
    advancedOpen,
    onAdvancedOpenChange,
    testChallenge,
    testRequiresSave,
    saveBlockers,
    editorIssueCount,
    onSave,
    onTestWebhook,
    onConfirmWebhook,
  } = props;
  return (
    <Overlay
      open
      onClose={onClose}
      closeDisabled={Boolean(busy)}
      size="lg"
      className="listener-editor-overlay"
      eyebrow="QUESTION REPLY POLICY"
      title={draft.id ? "编辑群监听" : "新增群监听"}
      footer={
        <div className="reply-drawer-footer">
          <span className={editorIssueCount ? "has-errors" : "is-ready"}>{editorIssueCount ? <><AlertTriangle size={13} />还有 {editorIssueCount} 项需要处理</> : <><CheckCircle2 size={13} />配置校验通过</>}</span>
          <div><Button variant="secondary" onClick={onClose} disabled={Boolean(busy)}>取消</Button><Button onClick={() => onSave()} disabled={Boolean(busy)}>{busy === "save" ? <LoaderCircle className="spin" size={13} /> : <Save size={13} />}保存监听器</Button></div>
        </div>
      }
    >
      <nav className="runtime-drawer-steps" aria-label="监听器配置步骤"><a href="#listener-basic"><span>1</span>监听对象</a><a href="#listener-engine"><span>2</span>引擎与 Runtime</a><a href="#listener-delivery"><span>3</span>发送方式</a><a href="#listener-budget"><span>4</span>预算</a></nav>
      <fieldset className="runtime-drawer-fields" disabled={Boolean(busy)}>
          <section className="runtime-form-section" id="listener-basic">
            <div className="runtime-form-heading"><UsersRound size={14} /><span><strong>监听对象</strong><small>一个监听器只能选择一个群聊；同一个群只能启用一个监听器。</small></span></div>
            <Field label="配置名称"><Input value={draft.name} placeholder={draft.groupName ? `${draft.groupName}自动答疑` : "例如：售后群自动答疑"} onChange={(event) => onDraftChange({ ...draft, name: event.target.value })} /></Field>
            <div className="single-group-picker">
              <div className="single-group-search"><Search size={13} /><input value={groupSearch} placeholder="搜索群聊" onChange={(event) => onGroupSearchChange(event.target.value)} /><span>只能选择一个群聊</span></div>
              <div className="single-group-list">
                {filteredGroups.map((group) => {
                  const id = String(group.id ?? group.conversation_id ?? "");
                  const name = String(group.name ?? group.display_name ?? id);
                  return <button type="button" className={draft.groupId === id ? "selected" : ""} key={id} onClick={() => {
                    const sameGroup = draft.groupId === id;
                    onDraftChange({
                      ...draft,
                      groupId: id,
                      groupName: name,
                      name: draft.name || `${name}自动答疑`,
                      webhookVerified: sameGroup && draft.webhookVerified,
                      deliveryMode: sameGroup ? draft.deliveryMode : "review",
                    });
                    onClearTestChallenge();
                  }}><span className="radio-dot">{draft.groupId === id && <Check size={10} />}</span><span><strong>{name}</strong><small>{id}</small></span></button>;
                })}
              </div>
            </div>
          </section>

          <section className="runtime-form-section" id="listener-engine">
            <div className="runtime-form-heading"><Bot size={14} /><span><strong>引擎与 Runtime</strong><small>选择 MCP 多轮 Agent，或让已发布的 Dify Chatflow 负责本轮检索与回答。</small></span></div>
            <div className="runtime-engine-note"><Sparkles size={14} /><span><strong>Agent V2</strong><small>事件时间线、只读工具策略与安全恢复默认启用；历史监听器保存后同样使用 Agent V2。</small></span></div>
            <div className="delivery-mode-grid answer-engine-grid">
              <button type="button" className={draft.answerEngine === "mcp" ? "selected" : ""} onClick={() => onDraftChange({ ...draft, answerEngine: "mcp" })}><Wrench size={16} /><span><strong>MCP + 提示词</strong><small>只读工具由后端自动授权并多轮补查，再由全局模型生成与复核。</small></span></button>
              <button type="button" className={draft.answerEngine === "dify" ? "selected" : ""} onClick={() => onDraftChange({ ...draft, answerEngine: "dify", difyAppId: draft.difyAppId || difyApps.find((app) => app.enabled)?.id || difyApps[0]?.id || "" })}><Bot size={16} /><span><strong>Dify Chatflow</strong><small>上传本轮附件并调用 Chatflow，后续安全流程保持不变。</small></span></button>
            </div>
            {draft.answerEngine === "dify" && <div className="dify-listener-choice">
              <Field label="Dify Chatflow 应用" hint="应用必须已发布；自动发送还要求启用且当前配置连接测试成功。">
                <select className="input" value={draft.difyAppId} onChange={(event) => onDraftChange({ ...draft, difyAppId: event.target.value })}>
                  <option value="">请选择一个 Dify Chatflow 应用</option>
                  {difyApps.map((app) => <option key={app.id} value={app.id}>{app.name}{app.enabled ? "" : "（已停用）"}</option>)}
                </select>
              </Field>
              {!difyApps.length && <div className="runtime-inline-empty"><CircleOff size={15} />尚未配置 Dify 应用，请先到“Dify 接入”工作台创建并测试。</div>}
              {selectedDifyApp && <div className={`dify-listener-state ${selectedDifyApp.connectionTestCurrent ? "is-ready" : "is-warning"}`}>
                {selectedDifyApp.connectionTestCurrent ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
                <span><strong>{selectedDifyApp.enabled ? selectedDifyApp.connectionTestCurrent ? "当前配置已验证" : "需要连接测试" : "应用已停用"}</strong><small>{selectedDifyApp.baseUrl.startsWith("http://") ? "HTTP 明文内网连接 · " : ""}参数测试只提供能力提示；实际附件格式与大小由上传接口最终校验。</small></span>
              </div>}
            </div>}
          </section>

          <section className="runtime-form-section">
            <div className="runtime-form-heading"><Bot size={14} /><span><strong>回答规则</strong><small>这里只控制回答范围和措辞，不能覆盖证据门槛和安全规则。</small></span></div>
            <Field label="系统提示词"><textarea className="input runtime-textarea" value={draft.systemPrompt} onChange={(event) => onDraftChange({ ...draft, systemPrompt: event.target.value })} /></Field>
            <Switch checked={draft.requireIndependentReview} onChange={(requireIndependentReview) => onDraftChange({ ...draft, requireIndependentReview })} label="独立证据复核" description="生成回答后再核对是否都有证据支持；关闭后跳过复核，但仍需满足现有证据门槛并保留审核后发送。" />
            {draft.requireIndependentReview && <div className="runtime-review-prompt">
              <div className="runtime-review-prompt-head"><span><strong>审查提示词</strong><small>留空使用默认提示词；固定安全规则始终生效，无法被覆盖。</small></span>{draft.reviewPrompt.trim() && <button type="button" className="runtime-reset-prompt" onClick={() => onDraftChange({ ...draft, reviewPrompt: "" })}>恢复默认</button>}</div>
              <textarea className="input runtime-textarea" rows={5} placeholder={DEFAULT_INDEPENDENT_REVIEW_PROMPT} value={draft.reviewPrompt} onChange={(event) => onDraftChange({ ...draft, reviewPrompt: event.target.value })} />
            </div>}
          </section>

          <section className="runtime-form-section" id="listener-delivery">
            <div className="runtime-form-heading"><Webhook size={14} /><span><strong>企微群机器人</strong><small>只用于消息推送。自动发送前必须完成一次群内可见测试。</small></span></div>
            {draft.id && draft.webhookConfigured && (
              <div className="runtime-segmented webhook-secret-mode"><button type="button" className={draft.webhookMode === "keep" ? "selected" : ""} onClick={() => onDraftChange({ ...draft, webhookMode: "keep", webhookUrl: "" })}>保留</button><button type="button" className={draft.webhookMode === "replace" ? "selected" : ""} onClick={() => { onDraftChange({ ...draft, webhookMode: "replace", webhookVerified: false, deliveryMode: "review" }); onClearTestChallenge(); }}>替换</button><button type="button" className={draft.webhookMode === "clear" ? "selected danger" : ""} onClick={() => { onDraftChange({ ...draft, webhookMode: "clear", webhookUrl: "", webhookVerified: false, deliveryMode: "review" }); onClearTestChallenge(); }}>清空</button></div>
            )}
            {(draft.webhookMode === "replace" || !draft.id) && <Field label="Webhook URL" hint="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"><Input type="password" value={draft.webhookUrl} placeholder="粘贴机器人 webhook" onChange={(event) => onDraftChange({ ...draft, webhookUrl: event.target.value, webhookConfigured: Boolean(event.target.value), webhookVerified: false })} /></Field>}
            <div className="webhook-verification-row">
              <span className={draft.webhookVerified ? "verified" : ""}>{draft.webhookVerified ? <CheckCircle2 size={14} /> : <ShieldAlert size={14} />}<span><strong>{draft.webhookVerified ? "已确认机器人属于所选群" : "尚未完成群归属确认"}</strong><small>HTTP 成功不能证明机器人发到了正确的群。</small></span></span>
              <Button variant="secondary" disabled={busy === "webhook-test" || busy === "save" || busy === "webhook-confirm"} onClick={() => onTestWebhook()}>{busy === "webhook-test" ? <LoaderCircle className="spin" size={12} /> : <Send size={12} />}{testRequiresSave ? "保存并发送测试" : "发送测试"}</Button>
            </div>
            {testChallenge && <div className="webhook-challenge"><span><strong>请在“{draft.groupName}”确认测试消息</strong><small>{testChallenge.code ? `随机码：${testChallenge.code}` : "确认看到刚才的测试消息后再继续。"}</small></span><Button onClick={() => onConfirmWebhook()} disabled={busy === "webhook-confirm"}>我已在该群看到</Button></div>}
            <div className="delivery-mode-grid">
              <button type="button" className={draft.deliveryMode === "review" ? "selected" : ""} onClick={() => onDraftChange({ ...draft, deliveryMode: "review" })}><Eye size={16} /><span><strong>审核后发送</strong><small>默认安全模式，可发送或放弃。</small></span></button>
              <button type="button" className={draft.deliveryMode === "automatic" ? "selected" : ""} onClick={() => {
                if (!draft.webhookVerified) return void toast.warning("自动发送需要先完成 webhook 群内可见确认");
                onDraftChange({ ...draft, deliveryMode: "automatic" });
              }}><Sparkles size={16} /><span><strong>自动发送</strong><small>真艾特不可解析时仍转入待发送。</small></span></button>
            </div>
          </section>

          <section className="runtime-form-section advanced-settings" id="listener-budget">
            <button type="button" className="advanced-toggle" onClick={() => onAdvancedOpenChange((value) => !value)}><span><Gauge size={14} /><span><strong>时序、{draft.answerEngine === "mcp" ? "Agent" : "Chatflow"} 与并发</strong><small>{draft.answerEngine === "mcp" ? "MCP 多轮查询" : "Dify 上传与工作流"}共享监听、群友等待和并发门禁。</small></span></span><ChevronDown size={14} className={advancedOpen ? "is-open" : ""} /></button>
            {advancedOpen && <div className="advanced-grid">
              <NumberField label="监听刷新间隔" suffix="秒" value={draft.tuning.pollIntervalSeconds} min={2} max={60} hint="只决定后台多久检查一次企微本地新消息；不是从发送到开始 MCP 检索的总耗时。" onChange={(value) => onDraftChange({ ...draft, tuning: { ...draft.tuning, pollIntervalSeconds: value } })} />
              <NumberField label="等待连续补充时长" suffix="秒" value={draft.tuning.sameSenderMergeSeconds} min={2} max={120} hint="仅在收集期内生效；同一人的有效补充会从最后一条重新计时。" onChange={(value) => onDraftChange({ ...draft, tuning: { ...draft.tuning, sameSenderMergeSeconds: value } })} />
              <NumberField label="留给群友回答的时间" suffix="秒" value={draft.tuning.humanReplyWaitSeconds} min={10} max={3600} hint="补充收集结束后开始计时。" onChange={(value) => onDraftChange({ ...draft, tuning: { ...draft.tuning, humanReplyWaitSeconds: value } })} />
              <NumberField label="个人上下文保留时间" suffix="分钟" value={Math.round(draft.tuning.sessionTimeoutSeconds / 60)} min={1} max={1440} hint="只保留同一人在同一群的历史。" onChange={(value) => onDraftChange({ ...draft, tuning: { ...draft.tuning, sessionTimeoutSeconds: value * 60 } })} />
              <NumberField label="同时检索问题数" suffix="个" value={draft.tuning.maxConcurrency} min={1} max={20} hint="同一个人始终串行。" onChange={(value) => onDraftChange({ ...draft, tuning: { ...draft.tuning, maxConcurrency: value } })} />
              {draft.answerEngine === "mcp" ? <>
                <NumberField label="单个问题 MCP 最长等待" suffix="秒" value={draft.tuning.mcpTimeoutSeconds} min={60} max={1800} hint="默认 900 秒；证据不足或超时都不发送。" onChange={(value) => onDraftChange({ ...draft, tuning: { ...draft.tuning, mcpTimeoutSeconds: value } })} />
                <NumberField label="Agent 最大查询轮数" suffix="轮" value={draft.tuning.maxAgentRounds} min={2} max={200} hint="默认 6 轮，最高 200 轮；空结果会要求 Agent 改写条件或切换工具，仍受总超时与 200 次工具调用预算限制。" onChange={(value) => onDraftChange({ ...draft, tuning: { ...draft.tuning, maxAgentRounds: value } })} />
                <NumberField label="Agent 最大工具调用" suffix="次" value={draft.tuning.maxToolCalls} min={1} max={200} hint="失败、非法参数和重复尝试也计入预算；达到上限后不再执行剩余调用。" onChange={(value) => onDraftChange({ ...draft, tuning: { ...draft.tuning, maxToolCalls: value } })} />
                <div className="agent-round-presets span-two"><span>轮次快捷值</span>{[6, 20, 50, 100, 200].map((rounds) => <button type="button" key={rounds} className={draft.tuning.maxAgentRounds === rounds ? "selected" : ""} onClick={() => onDraftChange({ ...draft, tuning: { ...draft.tuning, maxAgentRounds: rounds } })}>{rounds}</button>)}</div>
                {draft.tuning.maxAgentRounds > 50 && <div className="runtime-safety-note span-two"><AlertTriangle size={14} /><span><strong>高轮次预算</strong>模型和 MCP 费用、执行时间都会明显增加；总超时仍可能先结束任务。</span></div>}
              </> : <NumberField label="单个问题 Dify 最长等待" suffix="秒" value={draft.tuning.difyTimeoutSeconds} min={30} max={1800} hint="默认 300 秒；覆盖附件上传与完整 Chatflow SSE，超时不自动重试。" onChange={(value) => onDraftChange({ ...draft, tuning: { ...draft.tuning, difyTimeoutSeconds: value } })} />}
            </div>}
          </section>

          <Switch checked={draft.enabled} onChange={(enabled) => onDraftChange({ ...draft, enabled })} label="启用群监听" description="保存后立即应用；应用退出期间的消息不会补处理。" />
          {saveBlockers.length > 0 && draft.deliveryMode === "automatic" && <div className="runtime-safety-note is-danger"><ShieldAlert size={14} /><span><strong>自动发送尚未通过安全门</strong>{saveBlockers.join(" ")}</span></div>}
          </fieldset>
    </Overlay>
  );
}

function NumberField({ label, suffix, value, min, max, hint, onChange }: { label: string; suffix: string; value: number; min: number; max: number; hint: string; onChange: (value: number) => void }) {
  return <Field label={label} hint={`${hint} 范围 ${min}–${max}${suffix}。`}><div className="number-field"><Input type="number" min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} /><span>{suffix}</span></div></Field>;
}

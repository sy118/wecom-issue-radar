import { useState } from "react";
import { Copy, Eraser, LoaderCircle, Send, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { Button, Field, Input } from "../../components/ui";
import { toUserErrorMessage } from "../../lib/errors";
import { fillWechat, generateEqReply, supportsWechatFill } from "../../components/daily/api";
import {
  eqStyleLabel,
  normalizeEqResult,
  replyDrafts,
  sortEqStyles,
} from "../../components/daily/logic";
import type { EqReplyResult } from "../../components/daily/types";
import {
  DailyBusy,
  DailyError,
  DailyFieldset,
  DailyNotice,
  DailyPageHeader,
  copyText,
} from "../../components/daily/ui";
import "./daily.css";

/**
 * 回复助手：粘贴对方消息 → 潜台词解读 + 稳重/亲和/简短三版 + 发送前检查。
 * 只复制或填入微信输入框，永不点击发送。
 */
export function DailyReplyPage() {
  const [text, setText] = useState("");
  const [context, setContext] = useState("");
  const [showContext, setShowContext] = useState(false);
  const [recipient, setRecipient] = useState("");
  const [chatTitle, setChatTitle] = useState("");
  const [result, setResult] = useState<EqReplyResult | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [generating, setGenerating] = useState(false);
  const [filling, setFilling] = useState("");
  const [error, setError] = useState("");

  const generate = async () => {
    if (!text.trim()) {
      setError("先把对方的消息粘贴进来，再生成回复。");
      return;
    }
    setGenerating(true);
    setError("");
    try {
      const raw = await generateEqReply({
        text,
        context: showContext ? context : "",
        recipient,
      });
      const normalized = normalizeEqResult(raw);
      if (normalized.styles.length === 0) {
        setResult(null);
        setError("这次没有生成可用的草稿，可以稍后重试，或把对方消息缩短一点再试。");
        return;
      }
      setResult(normalized);
      setDrafts(replyDrafts(normalized));
    } catch (reason) {
      setResult(null);
      setError(toUserErrorMessage(reason, "生成失败，请稍后重试。"));
    } finally {
      setGenerating(false);
    }
  };

  const copy = async (content: string) => {
    if (!content.trim()) {
      toast.warning("这一版还是空的");
      return;
    }
    const copied = await copyText(content);
    if (copied) toast.success("已复制到剪贴板");
    else toast.error("复制失败，请手动选择文本复制");
  };

  const fill = async (styleId: string) => {
    const content = (drafts[styleId] ?? "").trim();
    if (!content) {
      toast.warning("这一版还是空的");
      return;
    }
    const title = chatTitle.trim();
    if (!title) {
      toast.warning("请先填写目标聊天标题", {
        description: "标题要和微信聊天窗口里显示的完全一致，只用于填入前校验。",
      });
      return;
    }
    if (!supportsWechatFill()) {
      const copied = await copyText(content);
      toast.warning("当前版本不支持填入微信", {
        description: copied ? "文案已复制，请手动粘贴到微信输入框。" : "请手动复制文案到微信输入框。",
      });
      return;
    }
    setFilling(styleId);
    try {
      const response = await fillWechat(title, content);
      toast.success(`已填入「${response.chatTitle}」`, {
        description: `${response.characters} 字；只填入不发送，请确认后自己点发送。`,
      });
    } catch (reason) {
      const copied = await copyText(content);
      toast.warning("填入失败，已降级为复制", {
        description: copied
          ? `${toUserErrorMessage(reason, "个人微信可能未连接。")} 文案已复制，可手动粘贴。`
          : toUserErrorMessage(reason, "个人微信可能未连接，请手动复制文案。"),
      });
    } finally {
      setFilling("");
    }
  };

  const reset = () => {
    setText("");
    setContext("");
    setResult(null);
    setDrafts({});
    setError("");
  };

  const styles = result ? sortEqStyles(result.styles) : [];
  const interpretation = result?.interpretation;
  const check = result?.check;

  return (
    <div className="page-content dl-page">
      <DailyPageHeader
        eyebrow="沟通助手"
        title="回复助手"
        description="把对方的消息粘进来，先看潜台词，再挑一版回复；只复制或填入，不会替你发送。"
        actions={
          <Button variant="ghost" onClick={reset} disabled={generating}>
            <Eraser size={15} /> 清空
          </Button>
        }
      />

      <section className="glass-card">
        <div className="dl-stack">
          <Field label="对方的消息" hint="可以连前后几句一起粘进来，解读会更准。">
            <textarea
              className="textarea"
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="例如：这个需求怎么又延期了？上周不是说能上吗？"
              rows={6}
            />
          </Field>
          <div className="dl-counter">{text.length} 字</div>

          <div className="dl-row">
            <Field label="对方称呼 / 身份（可选）" className="dl-grow">
              <Input
                value={recipient}
                onChange={(event) => setRecipient(event.target.value)}
                placeholder="例如：张总（直属领导）、王姐（客户对接人）"
              />
            </Field>
            <Field label="填入微信时的目标聊天标题" className="dl-grow">
              <Input
                value={chatTitle}
                onChange={(event) => setChatTitle(event.target.value)}
                placeholder="例如：客户-张先生"
              />
            </Field>
          </div>

          <div className="dl-row">
            <Button variant="secondary" onClick={() => setShowContext((value) => !value)}>
              {showContext ? "收起补充上下文" : "补充上下文（可选）"}
            </Button>
            <span className="dl-hint">填入只校验聊天标题和焦点，不点发送。</span>
          </div>

          {showContext && (
            <Field label="补充上下文" hint="例如前因后果、你希望达到的效果、不能承诺的底线。">
              <textarea
                className="textarea dl-textarea-short"
                value={context}
                onChange={(event) => setContext(event.target.value)}
                placeholder="可选：说明背景，措辞会更贴合场景。"
                rows={3}
              />
            </Field>
          )}

          <div className="dl-row">
            <Button onClick={() => void generate()} disabled={generating}>
              {generating ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}
              {generating ? "正在解读并起草…" : "解读潜台词并生成三版"}
            </Button>
            <span className="dl-hint">生成后可逐版修改，再复制或填入。</span>
          </div>
        </div>
      </section>

      {error && <DailyError message={error} onRetry={() => void generate()} />}

      {result?.fallback && (
        <DailyNotice tone="warning">
          AI 暂时不可用，已生成规则版通用措辞；可以直接修改使用，稍后再试一次可能更贴合语境。
        </DailyNotice>
      )}

      {result && (
        <section className="glass-card">
          <div className="dl-result-grid">
            <DailyFieldset
              title="潜台词解读"
              meta={interpretation?.signals.length ? `${interpretation.signals.length} 个信号` : undefined}
            >
              {interpretation?.summary ? (
                <p className="dl-summary">{interpretation.summary}</p>
              ) : (
                <p className="dl-muted">没有识别到明显的潜台词，按字面意思理解即可。</p>
              )}
              {interpretation && interpretation.signals.length > 0 && (
                <ul className="dl-signals">
                  {interpretation.signals.map((signal, index) => (
                    <li key={`${signal.label}-${index}`}>
                      <strong>{signal.label}</strong>
                      {signal.detail && <span>{signal.detail}</span>}
                    </li>
                  ))}
                </ul>
              )}
            </DailyFieldset>

            <DailyFieldset title="发送前检查">
              {check && check.risks.length > 0 && (
                <ul className="dl-check-list">
                  {check.risks.map((risk, index) => (
                    <li key={`risk-${index}`} className="dl-check-risk">
                      {risk}
                    </li>
                  ))}
                </ul>
              )}
              {check && check.suggestions.length > 0 && (
                <ul className="dl-check-list">
                  {check.suggestions.map((suggestion, index) => (
                    <li key={`suggestion-${index}`} className="dl-check-suggestion">
                      {suggestion}
                    </li>
                  ))}
                </ul>
              )}
              {(!check || (check.risks.length === 0 && check.suggestions.length === 0)) && (
                <p className="dl-muted">没有发现明显的语气风险，发送前再确认一次称呼和承诺即可。</p>
              )}
            </DailyFieldset>
          </div>
        </section>
      )}

      {styles.length > 0 && (
        <section className="glass-card">
          <div className="section-header">
            <div>
              <h2>三版回复</h2>
              <p>改到合适为止；填入前请确认目标聊天标题。</p>
            </div>
            <span className="dl-hint">逐版可编辑</span>
          </div>
          <div className="dl-style-grid">
            {styles.map((style) => (
              <article className="dl-style-card" key={style.id}>
                <div className="dl-style-head">
                  <h3>{eqStyleLabel(style.id)}</h3>
                  <span className="dl-badge">{style.title || eqStyleLabel(style.id)}</span>
                </div>
                <textarea
                  className="textarea dl-style-textarea"
                  value={drafts[style.id] ?? ""}
                  onChange={(event) =>
                    setDrafts((current) => ({ ...current, [style.id]: event.target.value }))
                  }
                  aria-label={`${eqStyleLabel(style.id)}版回复`}
                />
                <div className="dl-style-actions">
                  <Button variant="secondary" onClick={() => void copy(drafts[style.id] ?? "")}>
                    <Copy size={15} /> 复制
                  </Button>
                  <Button onClick={() => void fill(style.id)} disabled={filling === style.id}>
                    {filling === style.id ? <LoaderCircle className="spin" size={15} /> : <Send size={15} />}
                    {filling === style.id ? "填入中…" : "填入微信"}
                  </Button>
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      {generating && (
        <div className="dl-busy-row">
          <DailyBusy label="正在解读潜台词并起草三种风格…" />
        </div>
      )}

      <DailyNotice>
        只复制或填入微信输入框，不会点击发送。发送前请确认对象、称呼和内容都合适。
      </DailyNotice>
    </div>
  );
}

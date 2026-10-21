import { useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  Copy,
  LoaderCircle,
  Send,
  Sparkles,
  Wand2,
} from "lucide-react";
import { toast } from "sonner";

import { Button, Field, Input } from "../../components/ui";
import { toUserErrorMessage } from "../../lib/errors";
import { fillWechat, generateEqReply, supportsWechatFill } from "../../components/daily/api";
import { eqStyleLabel, sortEqStyles } from "../../components/daily/logic";
import type { EqReplyResult } from "../../components/daily/types";
import {
  DailyError,
  DailyNote,
  DailyPageHeader,
  copyText,
} from "../../components/daily/ui";

export function DailyEqReplyPage() {
  const [text, setText] = useState("");
  const [context, setContext] = useState("");
  const [showContext, setShowContext] = useState(false);
  const [chatTitle, setChatTitle] = useState("");
  const [result, setResult] = useState<EqReplyResult | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [generating, setGenerating] = useState(false);
  const [filling, setFilling] = useState("");
  const [error, setError] = useState("");

  const handleGenerate = async () => {
    const message = text.trim();
    if (!message) {
      toast.warning("先把对方的消息粘贴进来");
      return;
    }
    setGenerating(true);
    setError("");
    try {
      const response = await generateEqReply(message, context);
      setResult(response);
      const next: Record<string, string> = {};
      for (const style of sortEqStyles(response.styles ?? [])) {
        next[style.id] = style.content ?? "";
      }
      setDrafts(next);
    } catch (reason) {
      setError(toUserErrorMessage(reason, "生成失败，请稍后重试。"));
    } finally {
      setGenerating(false);
    }
  };

  const handleCopy = async (content: string) => {
    if (!content.trim()) {
      toast.warning("这一版还是空的");
      return;
    }
    const copied = await copyText(content);
    if (copied) toast.success("已复制到剪贴板");
    else toast.error("复制失败，请手动选择文本复制");
  };

  const handleFill = async (styleId: string) => {
    const content = (drafts[styleId] ?? "").trim();
    if (!content) {
      toast.warning("这一版还是空的");
      return;
    }
    const title = chatTitle.trim();
    if (!title) {
      toast.warning("请先填写目标聊天标题", {
        description: "标题需要和微信聊天窗口里显示的完全一致，只用于填入前的校验。",
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

  const styles = result ? sortEqStyles(result.styles ?? []) : [];

  return (
    <div className="page-content daily-page">
      <DailyPageHeader
        eyebrow="Daily Reply"
        icon={<Wand2 size={13} />}
        title="高情商回复"
        description="把对方的消息粘进来，生成稳重 / 亲和 / 简短三版草稿，改完再自己发送。"
      />

      <section className="glass-card">
        <div className="daily-field-stack">
          <Field label="对方的消息">
            <textarea
              className="textarea"
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="把对方的消息粘贴到这里，可以包含前后几句。"
              rows={6}
            />
          </Field>
          <div className="daily-char-count">{text.length} 字</div>

          <div className="daily-actions">
            <Button variant="secondary" onClick={() => setShowContext((value) => !value)}>
              <ChevronDown size={15} className={showContext ? "is-open" : undefined} />
              {showContext ? "收起补充上下文" : "补充上下文（可选）"}
            </Button>
          </div>
          {showContext && (
            <Field label="补充上下文" hint="例如前因后果、和对方的关系、你希望达到的效果。">
              <textarea
                className="textarea"
                value={context}
                onChange={(event) => setContext(event.target.value)}
                placeholder="可选：说明背景，让措辞更贴合场景。"
                rows={3}
              />
            </Field>
          )}

          <Field label="填入微信时的目标聊天标题" hint="需要和微信聊天窗口标题完全一致；只用于填入前校验。">
            <Input
              value={chatTitle}
              onChange={(event) => setChatTitle(event.target.value)}
              placeholder="例如：客户-张先生"
            />
          </Field>

          <div className="daily-actions">
            <Button onClick={() => void handleGenerate()} disabled={generating}>
              {generating ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}
              {generating ? "正在生成…" : "生成三种风格"}
            </Button>
            <span className="daily-hint">生成后可以逐版修改、复制，或填入微信输入框。</span>
          </div>
        </div>
      </section>

      {error && <DailyError message={error} onRetry={() => void handleGenerate()} />}

      {result?.fallback && (
        <div className="daily-warning">
          <AlertTriangle size={16} />
          <span>AI 暂不可用，已生成通用模板；你可以直接修改后使用，稍后再试一次可能更贴合语境。</span>
        </div>
      )}

      {result && styles.length === 0 && (
        <DailyError
          message="这次没有生成可用的草稿，可能是模型暂时不可用。可以稍后重试，或先把消息改写得更简短再生成。"
          onRetry={() => void handleGenerate()}
        />
      )}

      {result && styles.length > 0 && (
        <section className="glass-card">
          <div className="daily-style-grid">
            {styles.map((style) => (
              <article className="daily-style-card" key={style.id}>
                <div className="daily-style-head">
                  <h3>{eqStyleLabel(style.id)}</h3>
                  <span className="daily-badge daily-badge-muted">{style.title || eqStyleLabel(style.id)}</span>
                </div>
                <textarea
                  className="textarea"
                  value={drafts[style.id] ?? ""}
                  onChange={(event) =>
                    setDrafts((current) => ({ ...current, [style.id]: event.target.value }))
                  }
                  aria-label={`${eqStyleLabel(style.id)}版回复`}
                />
                <div className="daily-actions">
                  <Button variant="secondary" onClick={() => void handleCopy(drafts[style.id] ?? "")}>
                    <Copy size={15} />复制
                  </Button>
                  <Button onClick={() => void handleFill(style.id)} disabled={filling === style.id}>
                    {filling === style.id ? <LoaderCircle className="spin" size={15} /> : <Send size={15} />}
                    {filling === style.id ? "填入中…" : "填入微信"}
                  </Button>
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      <DailyNote>只复制或填入微信输入框，不会点击发送。发送前请确认对象、称呼和内容都合适。</DailyNote>
    </div>
  );
}

import { useState } from "react";
import { Copy, Download, Eraser, FileText, LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Button, Field } from "../../components/ui";
import { toUserErrorMessage } from "../../lib/errors";
import { summarizeText } from "../../components/daily/api";
import {
  DIGEST_MODE_HINTS,
  DIGEST_MODE_LABELS,
  composeDigestMarkdown,
  digestDrafts,
  digestMarkdownFilename,
  normalizeSummarize,
} from "../../components/daily/logic";
import type { DigestDrafts, DigestMode, SummarizeResult } from "../../components/daily/types";
import {
  DailyBusy,
  DailyError,
  DailyFieldset,
  DailyNotice,
  DailyPageHeader,
  copyText,
  downloadMarkdown,
} from "../../components/daily/ui";
import "./daily.css";

/**
 * 长文速读：粘贴长消息/会议记录，输出摘要、要点、风险、下一步，
 * 可编辑后复制或导出 Markdown。
 */
export function DailyDigestPage() {
  const [text, setText] = useState("");
  const [mode, setMode] = useState<DigestMode>("digest");
  const [result, setResult] = useState<SummarizeResult | null>(null);
  const [drafts, setDrafts] = useState<DigestDrafts | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const generate = async () => {
    if (!text.trim()) {
      setError("先把要速读的长文或会议记录粘贴进来。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const raw = await summarizeText(text, mode);
      const normalized = normalizeSummarize(raw);
      setResult(normalized);
      setDrafts(digestDrafts(normalized));
    } catch (reason) {
      setResult(null);
      setDrafts(null);
      setError(toUserErrorMessage(reason, "整理失败，请稍后重试。"));
    } finally {
      setBusy(false);
    }
  };

  const update = (key: keyof DigestDrafts, value: string) => {
    setDrafts((current) => (current ? { ...current, [key]: value } : current));
  };

  const copy = async () => {
    if (!drafts) return;
    const content = composeDigestMarkdown({ mode, drafts });
    const copied = await copyText(content);
    if (copied) toast.success("Markdown 已复制");
    else toast.error("复制失败，请手动选择文本复制");
  };

  const exportMarkdown = () => {
    if (!drafts) return;
    const content = composeDigestMarkdown({ mode, drafts });
    const saved = downloadMarkdown(digestMarkdownFilename(mode), content);
    if (saved) toast.success("已导出 Markdown");
    else toast.error("导出失败，可以改用复制");
  };

  const reset = () => {
    setText("");
    setResult(null);
    setDrafts(null);
    setError("");
  };

  return (
    <div className="page-content dl-page">
      <DailyPageHeader
        eyebrow="沟通助手"
        title="长文速读"
        description="粘贴长消息或会议记录，先出结论、要点、风险与下一步，再按自己的话改一版。"
        actions={
          <Button variant="ghost" onClick={reset} disabled={busy}>
            <Eraser size={15} /> 清空
          </Button>
        }
      />

      <section className="glass-card">
        <div className="dl-stack">
          <div className="dl-mode-tabs" role="tablist" aria-label="速读模式">
            {(Object.keys(DIGEST_MODE_LABELS) as DigestMode[]).map((item) => (
              <button
                key={item}
                type="button"
                role="tab"
                aria-selected={mode === item}
                className={mode === item ? "is-active" : undefined}
                onClick={() => setMode(item)}
              >
                <FileText size={14} /> {DIGEST_MODE_LABELS[item]}
              </button>
            ))}
          </div>
          <p className="dl-hint">{DIGEST_MODE_HINTS[mode]}</p>

          <Field label="原始长文 / 会议记录">
            <textarea
              className="textarea dl-digest-input"
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="把聊天长记录、公告、邮件或会议纪要粘贴到这里。"
              rows={10}
            />
          </Field>
          <div className="dl-counter">{text.length} 字</div>

          <div className="dl-row">
            <Button onClick={() => void generate()} disabled={busy}>
              {busy ? <LoaderCircle className="spin" size={16} /> : <FileText size={16} />}
              {busy ? "正在整理…" : "开始速读"}
            </Button>
            <span className="dl-hint">结果可以直接改，导出前不会外发。</span>
          </div>
        </div>
      </section>

      {error && <DailyError message={error} onRetry={() => void generate()} />}

      {busy && (
        <div className="dl-busy-row">
          <DailyBusy label="正在提炼摘要、要点、风险与下一步…" />
        </div>
      )}

      {result?.fallback && (
        <DailyNotice tone="warning">
          AI 暂时不可用，已用规则整理出基础结构；可以自己补充要点，稍后再试一次。
        </DailyNotice>
      )}

      {drafts && (
        <section className="glass-card">
          <div className="section-header">
            <div>
              <h2>整理结果</h2>
              <p>以下内容可逐项编辑；要点、风险、下一步每行一条。</p>
            </div>
            <div className="dl-head-actions">
              <Button variant="secondary" onClick={() => void copy()}>
                <Copy size={15} /> 复制 Markdown
              </Button>
              <Button variant="secondary" onClick={exportMarkdown}>
                <Download size={15} /> 导出 Markdown
              </Button>
            </div>
          </div>

          <div className="dl-digest-grid">
            <DailyFieldset title="摘要">
              <textarea
                className="textarea dl-summary-textarea"
                value={drafts.summary}
                onChange={(event) => update("summary", event.target.value)}
                aria-label="摘要"
                placeholder="一句话结论…"
              />
            </DailyFieldset>
            <DailyFieldset title="要点" meta={<span className="dl-hint">{countLines(drafts.points)} 条</span>}>
              <textarea
                className="textarea"
                value={drafts.points}
                onChange={(event) => update("points", event.target.value)}
                aria-label="要点"
                placeholder="每行一条要点"
              />
            </DailyFieldset>
            <DailyFieldset title="风险" meta={<span className="dl-hint">{countLines(drafts.risks)} 条</span>}>
              <textarea
                className="textarea"
                value={drafts.risks}
                onChange={(event) => update("risks", event.target.value)}
                aria-label="风险"
                placeholder="每行一条风险（没有可以留空）"
              />
            </DailyFieldset>
            <DailyFieldset title="下一步" meta={<span className="dl-hint">{countLines(drafts.next)} 条</span>}>
              <textarea
                className="textarea"
                value={drafts.next}
                onChange={(event) => update("next", event.target.value)}
                aria-label="下一步"
                placeholder="每行一条待办/下一步"
              />
            </DailyFieldset>
          </div>
        </section>
      )}

      <DailyNotice>
        速读结果只在本机生成与保存；导出的是 Markdown 文本文件，不会自动上传到任何文档平台。
      </DailyNotice>
    </div>
  );
}

function countLines(value: string): number {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean).length;
}

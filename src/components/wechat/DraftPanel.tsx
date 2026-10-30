import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  Clipboard,
  CornerDownLeft,
  Feather,
  LoaderCircle,
  RefreshCw,
  Settings2,
  Sparkles,
} from "lucide-react";
import clsx from "clsx";

import { draftStatusCopy } from "../../pages/wechat/logic";
import type { ReplyStyle, WechatDraft, WechatProject } from "../../pages/wechat/types";

export interface DraftPanelProps {
  project?: WechatProject;
  draft?: WechatDraft;
  stale: boolean;
  generating: boolean;
  rewriting: string;
  saving: boolean;
  filling: boolean;
  providerName: string;
  /** User-editable rewrite presets from the configuration center. */
  rewriteStyles: ReplyStyle[];
  onGenerate: () => void;
  onRewrite: (style: ReplyStyle) => void;
  onSave: (content: string) => void;
  onCopy: (content: string) => void;
  onFill: (content: string) => void;
  onOpenConfig: () => void;
}

export function DraftPanel({
  project,
  draft,
  stale,
  generating,
  rewriting,
  saving,
  filling,
  providerName,
  rewriteStyles,
  onGenerate,
  onRewrite,
  onSave,
  onCopy,
  onFill,
  onOpenConfig,
}: DraftPanelProps) {
  const [content, setContent] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setContent(draft?.content ?? "");
  }, [draft?.content, project?.id]);

  if (!project) {
    return (
      <aside className="wechat-draft wechat-draft-empty">
        <Feather size={28} />
        <strong>回复草稿</strong>
        <span>选择项目后，生成的草稿会保留在这里；你可以反复修改，再复制或填入微信。</span>
      </aside>
    );
  }

  const generatingNow = generating || draft?.status === "generating";
  const statusLabel = draftStatusCopy(draft, stale);
  const statusTone = stale || draft?.status === "error"
    ? "is-warn"
    : draft?.content
      ? "is-good"
      : "is-muted";
  const dirty = content !== (draft?.content ?? "");
  const rewriteButtons = rewriteStyles;

  return (
    <aside className="wechat-draft" aria-label="回复草稿面板">
      <header className="wechat-draft-header">
        <div>
          <span className="eyebrow">REPLY DRAFT</span>
          <h2>回复草稿</h2>
        </div>
        <span className={clsx("wechat-status-chip", statusTone)}>{statusLabel}</span>
      </header>

      <div className="wechat-draft-context">
        <div>
          <span>项目</span>
          <strong>{project.name}</strong>
        </div>
        <div>
          <span>口吻</span>
          <strong>{project.tone || "自然友好"}</strong>
        </div>
        <div>
          <span>模型</span>
          <strong>{providerName}</strong>
        </div>
      </div>

      {project.summary && (
        <p className="wechat-draft-summary">{project.summary}</p>
      )}

      {stale && (
        <div className="wechat-draft-warning">
          <AlertTriangle size={15} />
          <span>会话有了新消息，当前草稿可能已过期。建议重新生成后再发送。</span>
          <button type="button" onClick={onGenerate} disabled={generatingNow}>重新生成</button>
        </div>
      )}

      {draft?.status === "error" && draft.errorMessage && (
        <div className="wechat-draft-warning is-danger">
          <AlertTriangle size={15} />
          <span>{draft.errorMessage}</span>
        </div>
      )}

      <div className={clsx("wechat-draft-editor", generatingNow && "is-generating")}>
        {generatingNow && (
          <div className="wechat-draft-generating">
            <LoaderCircle className="spin" size={22} />
            <span>正在结合会话上下文与项目规则生成…</span>
          </div>
        )}
        <textarea
          className="wechat-draft-textarea"
          aria-label="回复草稿"
          value={content}
          placeholder="点击“生成草稿”，或直接在这里输入回复；内容只保存在本机。"
          onChange={(event) => setContent(event.target.value)}
          onBlur={() => {
            if (dirty) onSave(content);
          }}
        />
        <div className="wechat-draft-count">
          <span>{content.length} 字</span>
          {saving && <span><LoaderCircle className="spin" size={12} />保存中</span>}
          {!saving && dirty && <span>失焦后自动保存</span>}
        </div>
      </div>

      <div className="wechat-rewrite-grid" role="group" aria-label="改写草稿风格">
        {rewriteButtons.map((button) => (
          <button
            type="button"
            key={button.id}
            className="button button-secondary"
            title={button.instruction}
            disabled={generatingNow || !draft?.content || Boolean(rewriting)}
            onClick={() => onRewrite(button)}
          >
            {rewriting === button.id
              ? <LoaderCircle className="spin" size={14} />
              : button.id === "another" ? <RefreshCw size={14} /> : null}
            {button.label}
          </button>
        ))}
      </div>

      <div className="wechat-draft-actions">
        <button
          type="button"
          className="button button-primary wechat-draft-generate"
          disabled={generatingNow}
          aria-busy={generatingNow}
          onClick={onGenerate}
        >
          {generatingNow ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}
          {draft?.content ? "重新生成" : "生成草稿"}
        </button>
        <button
          type="button"
          className="button button-secondary"
          disabled={!content.trim()}
          onClick={() => {
            onCopy(content);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1400);
          }}
        >
          {copied ? <Check size={16} /> : <Clipboard size={16} />}
          {copied ? "已复制" : "复制"}
        </button>
        <button
          type="button"
          className="button button-secondary"
          disabled={!content.trim() || filling || generatingNow}
          onClick={() => onFill(content)}
        >
          {filling ? <LoaderCircle className="spin" size={16} /> : <CornerDownLeft size={16} />}
          {filling ? "正在填入" : "填入微信"}
        </button>
      </div>

      <button type="button" className="wechat-draft-settings" onClick={onOpenConfig}>
        <Settings2 size={14} />
        打开配置中心（模型、回复风格、Skill 与 MCP）
      </button>
    </aside>
  );
}

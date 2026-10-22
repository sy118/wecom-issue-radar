import { useEffect, useId, useMemo, useState } from "react";
import {
  AlertCircle,
  Check,
  FolderOpen,
  LoaderCircle,
  MessageCircle,
  Search,
  Trash2,
  Users,
  X,
} from "lucide-react";
import clsx from "clsx";

import { Field, Input, Switch } from "../ui";
import { useDialogA11y } from "../../lib/useDialogA11y";
import type { ProjectEditorState } from "../../pages/wechat/useWechatWorkbench";
import {
  TONE_PRESETS,
  newWechatProject,
  type ChatKind,
  type TriggerMode,
  type WechatChat,
  type WechatMcpServer,
  type WechatProject,
  type WechatProjectDefaults,
  type WechatProvider,
} from "../../pages/wechat/types";

export interface ProjectEditorDrawerProps {
  state: ProjectEditorState | null;
  chats: WechatChat[];
  chatsLoading: boolean;
  chatsError: string;
  providers: WechatProvider[];
  mcpServers: WechatMcpServer[];
  /** Defaults from the configuration center for brand-new projects. */
  projectDefaults?: WechatProjectDefaults;
  saving: boolean;
  onClose: () => void;
  onReloadChats: () => void;
  onSave: (project: WechatProject) => Promise<void>;
  onDelete: (project: WechatProject) => void;
}

const triggerModes: Array<{ value: TriggerMode; label: string; hint: string }> = [
  { value: "auto", label: "自动", hint: "有新消息就准备草稿" },
  { value: "manual", label: "手动", hint: "仅在点击生成时读取" },
  { value: "rules", label: "按规则", hint: "关键词或 @我 才触发" },
];

export function ProjectEditorDrawer({
  state,
  chats,
  chatsLoading,
  chatsError,
  providers,
  mcpServers,
  projectDefaults,
  saving,
  onClose,
  onReloadChats,
  onSave,
  onDelete,
}: ProjectEditorDrawerProps) {
  const [draft, setDraft] = useState<WechatProject | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    setDraft(state?.project ? structuredClone(state.project) : null);
    setQuery("");
  }, [state?.mode, state?.project?.id]);

  const filteredChats = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return chats;
    return chats.filter((chat) => (
      chat.displayName.toLowerCase().includes(keyword)
      || chat.chatId.toLowerCase().includes(keyword)
    ));
  }, [chats, query]);

  const titleId = useId();
  const panelRef = useDialogA11y<HTMLElement>({
    open: Boolean(state),
    onClose,
    // A save in flight owns the dialog: Escape and the close affordances wait.
    closeDisabled: saving,
    initialFocusWithin: ".wechat-drawer-body",
  });

  if (!state) return null;

  const selectChat = (chat: WechatChat) => {
    setDraft(newWechatProject(chat, providers, {
      tone: projectDefaults?.tone,
      triggerMode: projectDefaults?.triggerMode,
    }));
    setQuery("");
  };

  const update = <K extends keyof WechatProject>(key: K, value: WechatProject[K]) => {
    setDraft((current) => (current ? { ...current, [key]: value } : current));
  };
  const updateTrigger = <K extends keyof WechatProject["trigger"]>(
    key: K,
    value: WechatProject["trigger"][K],
  ) => {
    setDraft((current) => (
      current ? { ...current, trigger: { ...current.trigger, [key]: value } } : current
    ));
  };

  const save = async () => {
    if (!draft || !draft.name.trim() || !draft.chatId) return;
    try {
      await onSave(draft);
    } catch {
      // The page-level handler already reported the concrete error.
    }
  };

  return (
    <div className="modal-backdrop">
      <section
        ref={panelRef}
        className="wechat-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <header className="wechat-drawer-header">
          <div>
            <span className="eyebrow">PROJECT CONTEXT</span>
            <h2 id={titleId}>{state.mode === "edit" ? "编辑会话项目" : draft ? "新建会话项目" : "选择微信会话"}</h2>
          </div>
          <button
            type="button"
            className="wechat-icon-button"
            aria-label="关闭"
            disabled={saving}
            onClick={onClose}
          >
            <X size={17} />
          </button>
        </header>

        <div className="wechat-drawer-body">
          {!draft ? (
            <div className="wechat-chat-picker">
              <div className="wechat-search-field">
                <Search size={15} />
                <Input
                  value={query}
                  placeholder="搜索备注、昵称或 wxid"
                  onChange={(event) => setQuery(event.target.value)}
                />
              </div>
              {chatsError && (
                <div className="wechat-inline-error">
                  <AlertCircle size={14} />
                  <span>{chatsError}</span>
                  <button type="button" onClick={onReloadChats}>重试</button>
                </div>
              )}
              {chatsLoading && (
                <div className="wechat-picker-state"><LoaderCircle className="spin" size={20} />正在安全读取微信会话…</div>
              )}
              {!chatsLoading && !filteredChats.length && (
                <div className="wechat-picker-state">
                  <MessageCircle size={24} />
                  <strong>没有可选择的会话</strong>
                  <span>请先在设置中完成微信检测与密钥扫描。</span>
                </div>
              )}
              <div className="wechat-chat-list">
                {filteredChats.map((chat) => (
                  <button type="button" key={`${chat.accountId}:${chat.chatId}`} onClick={() => selectChat(chat)}>
                    <span className={clsx("wechat-chat-avatar", `is-${chat.kind}`)}>
                      {chat.kind === "group" ? <Users size={16} /> : chat.displayName.slice(0, 1)}
                    </span>
                    <span className="wechat-chat-copy">
                      <strong>{chat.displayName}</strong>
                      <small>{chat.lastMessage || chat.chatId}</small>
                    </span>
                    {chat.unread > 0 && <span className="wechat-unread-badge">{chat.unread > 99 ? "99+" : chat.unread}</span>}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="wechat-project-form">
              <section className="wechat-form-section">
                <h3>项目身份</h3>
                <div className="form-grid two-columns">
                  <Field label="项目名称">
                    <Input value={draft.name} onChange={(event) => update("name", event.target.value)} />
                  </Field>
                  <Field label="微信聊天标题" hint="用于“填入微信”前的安全匹配；微信备注变化后请同步修改。">
                    <Input value={draft.chatTitle} onChange={(event) => update("chatTitle", event.target.value)} />
                  </Field>
                  <Field label="会话类型">
                    <select
                      className="input"
                      value={draft.kind}
                      onChange={(event) => update("kind", event.target.value as ChatKind)}
                    >
                      <option value="direct">单聊</option>
                      <option value="group">群聊</option>
                    </select>
                  </Field>
                  <Field label="稳定会话标识" hint="只读；用于读取本地数据库中的同一会话。">
                    <Input value={draft.chatId} disabled />
                  </Field>
                </div>
                {state.mode === "new" && (
                  <button type="button" className="wechat-text-button" onClick={() => setDraft(null)}>
                    更换会话
                  </button>
                )}
              </section>

              <section className="wechat-form-section">
                <h3>回复上下文</h3>
                <Field label="项目摘要" hint="长期背景，例如对方角色与正在推进的事情；不要在这里保存密钥。">
                  <textarea
                    className="wechat-form-textarea"
                    rows={4}
                    value={draft.summary}
                    onChange={(event) => update("summary", event.target.value)}
                    placeholder="例如：对方负责设计交付，我们正在确认下周排期…"
                  />
                </Field>
                <Field label="口吻">
                  <select
                    className="input"
                    value={draft.tone}
                    onChange={(event) => update("tone", event.target.value)}
                  >
                    {!TONE_PRESETS.includes(draft.tone) && <option value={draft.tone}>{draft.tone}</option>}
                    {TONE_PRESETS.map((tone) => <option key={tone} value={tone}>{tone}</option>)}
                  </select>
                </Field>
                <Field label="额外提示词" hint="描述回复原则；留空时使用安全默认规则。">
                  <textarea
                    className="wechat-form-textarea"
                    rows={4}
                    value={draft.prompt}
                    onChange={(event) => update("prompt", event.target.value)}
                    placeholder="例如：自然简洁；不确定时先澄清，不要替我承诺具体日期。"
                  />
                </Field>
              </section>

              <section className="wechat-form-section">
                <h3>触发规则</h3>
                <div className="wechat-segmented" role="group" aria-label="触发方式">
                  {triggerModes.map((mode) => (
                    <button
                      type="button"
                      key={mode.value}
                      className={clsx(draft.trigger.mode === mode.value && "is-active")}
                      onClick={() => updateTrigger("mode", mode.value)}
                    >
                      <strong>{mode.label}</strong>
                      <small>{mode.hint}</small>
                    </button>
                  ))}
                </div>
                {draft.trigger.mode === "rules" && (
                  <div className="form-grid two-columns">
                    <Field label="关键词" hint="多个关键词用逗号分隔。">
                      <Input
                        value={draft.trigger.keywords.join(", ")}
                        onChange={(event) => updateTrigger(
                          "keywords",
                          event.target.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
                        )}
                      />
                    </Field>
                    <Switch
                      checked={draft.trigger.mentionMe}
                      onChange={(mentionMe) => updateTrigger("mentionMe", mentionMe)}
                      label="可靠解析到 @我 时也触发"
                    />
                  </div>
                )}
              </section>

              <section className="wechat-form-section">
                <h3>模型、Skill 与 MCP</h3>
                <Field label="回复模型" hint="在全局设置中维护模型连接与密钥。">
                  <select
                    className="input"
                    value={draft.providerId}
                    onChange={(event) => update("providerId", event.target.value)}
                  >
                    <option value="">尚未选择</option>
                    {providers.map((provider) => (
                      <option key={provider.id} value={provider.id}>
                        {provider.name}{provider.model ? ` · ${provider.model}` : ""}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Skill 目录（只读）" hint="每行一个包含 SKILL.md 的目录；由你手动填写，不做自动扫描。">
                  <textarea
                    className="wechat-form-textarea"
                    rows={3}
                    value={draft.skillPaths.join("\n")}
                    onChange={(event) => update(
                      "skillPaths",
                      event.target.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
                    )}
                    placeholder="C:\\path\\to\\skill"
                  />
                </Field>
                <div className="wechat-mcp-picker">
                  <span className="wechat-form-label">启用的 MCP 服务</span>
                  {mcpServers.length ? mcpServers.map((server) => (
                    <label key={server.id} className="wechat-check-row">
                      <input
                        type="checkbox"
                        checked={draft.mcpServerIds.includes(server.id)}
                        onChange={(event) => update(
                          "mcpServerIds",
                          event.target.checked
                            ? [...draft.mcpServerIds, server.id]
                            : draft.mcpServerIds.filter((id) => id !== server.id),
                        )}
                      />
                      <span>
                        <strong>{server.name}</strong>
                        <small>{server.toolCount} 个工具{server.lastError ? " · 最近测试失败" : ""}</small>
                      </span>
                    </label>
                  )) : (
                    <p className="wechat-form-hint">
                      <FolderOpen size={14} />尚未发现 MCP 服务；可稍后在全局设置中配置。
                    </p>
                  )}
                </div>
                <Switch
                  checked={draft.enabled}
                  onChange={(enabled) => update("enabled", enabled)}
                  label="启用项目"
                  description="停用后保留历史与草稿，但不再准备新草稿。"
                />
              </section>
            </div>
          )}
        </div>

        {draft && (
          <footer className="wechat-drawer-footer">
            {state.mode === "edit" && (
              <button type="button" className="button button-danger" onClick={() => onDelete(draft)}>
                <Trash2 size={15} />删除项目
              </button>
            )}
            <span />
            <button type="button" className="button button-secondary" disabled={saving} onClick={onClose}>取消</button>
            <button
              type="button"
              className="button button-primary"
              disabled={saving || !draft.name.trim() || !draft.chatId}
              onClick={() => void save()}
            >
              {saving ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}
              {saving ? "保存中" : "保存项目"}
            </button>
          </footer>
        )}
      </section>
    </div>
  );
}

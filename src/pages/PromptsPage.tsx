import { useEffect, useState } from "react";
import { Check, Copy, MessageSquareText, Plus, Save, Star, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { toUserErrorMessage } from "../lib/errors";
import type { AppConfig, PromptItem } from "../types";
import { Button, Field, Input, SectionHeader } from "../components/ui";

const createPrompt = (): PromptItem => ({
  id: `custom_${Date.now()}`,
  name: "新提示词",
  description: "说明这个提示词适合什么分析场景",
  content: "请描述你希望大模型如何分析当天群聊，以及需要输出哪些内容。",
});

export function PromptsPage({
  config,
  onSave,
}: {
  config: AppConfig;
  onSave: (config: AppConfig) => Promise<void>;
}) {
  const [draft, setDraft] = useState<AppConfig>(config);
  const [selectedId, setSelectedId] = useState(config.prompts.default_id);
  const [saving, setSaving] = useState(false);

  useEffect(() => setDraft(config), [config]);
  const selected = draft.prompts.items.find((item) => item.id === selectedId) ?? draft.prompts.items[0];

  const updateSelected = (patch: Partial<PromptItem>) => {
    setDraft((previous) => ({
      ...previous,
      prompts: {
        ...previous.prompts,
        items: previous.prompts.items.map((item) => item.id === selected.id ? { ...item, ...patch } : item),
      },
    }));
  };

  const addPrompt = () => {
    const prompt = createPrompt();
    setDraft((previous) => ({ ...previous, prompts: { ...previous.prompts, items: [...previous.prompts.items, prompt] } }));
    setSelectedId(prompt.id);
  };

  const duplicatePrompt = () => {
    const prompt = { ...selected, id: `custom_${Date.now()}`, name: `${selected.name}（副本）` };
    setDraft((previous) => ({ ...previous, prompts: { ...previous.prompts, items: [...previous.prompts.items, prompt] } }));
    setSelectedId(prompt.id);
  };

  const removePrompt = () => {
    if (draft.prompts.items.length <= 1) return;
    const remaining = draft.prompts.items.filter((item) => item.id !== selected.id);
    const nextDefault = draft.prompts.default_id === selected.id ? remaining[0].id : draft.prompts.default_id;
    setDraft((previous) => ({ ...previous, prompts: { default_id: nextDefault, items: remaining } }));
    setSelectedId(remaining[0].id);
  };

  const save = async () => {
    setSaving(true);
    try {
      await onSave(draft);
      toast.success("提示词已保存");
    } catch (error) {
      toast.error("提示词保存失败", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page-content">
      <div className="page-title-row">
        <div><div className="eyebrow"><MessageSquareText size={13} />Prompt Library</div><h1>提示词</h1><p>为不同业务场景准备多套分析规则，处理时使用默认提示词。</p></div>
        <Button onClick={() => void save()} disabled={saving}><Save size={16} />{saving ? "保存中" : "保存更改"}</Button>
      </div>
      <div className="prompt-layout">
        <section className="glass-card prompt-list-panel">
          <SectionHeader title={`提示词库 · ${draft.prompts.items.length}`} action={<Button variant="ghost" onClick={addPrompt}><Plus size={16} /></Button>} />
          <div className="prompt-list">
            {draft.prompts.items.map((item) => (
              <button key={item.id} className={item.id === selected.id ? "prompt-list-item active" : "prompt-list-item"} onClick={() => setSelectedId(item.id)}>
                <span className="prompt-list-icon"><MessageSquareText size={17} /></span>
                <span><strong>{item.name}</strong><small>{item.description || "暂无说明"}</small></span>
                {item.id === draft.prompts.default_id && <span className="default-chip"><Star size={10} fill="currentColor" />默认</span>}
              </button>
            ))}
          </div>
          <Button variant="secondary" className="add-prompt-button" onClick={addPrompt}><Plus size={15} />添加提示词</Button>
        </section>

        <section className="glass-card prompt-editor">
          <SectionHeader
            title="编辑提示词"
            description={`标识：${selected.id}`}
            action={<div className="inline-actions"><Button variant="ghost" title="复制" onClick={duplicatePrompt}><Copy size={15} /></Button><Button variant="ghost" title="删除" onClick={removePrompt} disabled={draft.prompts.items.length <= 1}><Trash2 size={15} /></Button></div>}
          />
          <div className="form-grid">
            <Field label="名称"><Input value={selected.name} onChange={(event) => updateSelected({ name: event.target.value })} /></Field>
            <Field label="使用说明" hint="用于帮助业务人员判断该提示词的适用场景。"><Input value={selected.description} onChange={(event) => updateSelected({ description: event.target.value })} /></Field>
            <Field label="提示词内容" hint="系统会自动附加所选日期、群名、输出结构和字段约束。">
              <textarea className="textarea prompt-textarea" value={selected.content} onChange={(event) => updateSelected({ content: event.target.value })} />
            </Field>
          </div>
          <button
            className={selected.id === draft.prompts.default_id ? "default-selector selected" : "default-selector"}
            onClick={() => setDraft((previous) => ({ ...previous, prompts: { ...previous.prompts, default_id: selected.id } }))}
          >
            <span className="radio-mark">{selected.id === draft.prompts.default_id && <Check size={13} />}</span>
            <span><strong>设为默认分析提示词</strong><small>开始处理页面会自动使用它</small></span>
          </button>
        </section>
      </div>
    </div>
  );
}

import { useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Check,
  Copy,
  ListChecks,
  MessageSquareText,
  Plus,
  Save,
  Star,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { toUserErrorMessage } from "../lib/errors";
import type {
  AppConfig,
  IssueFieldDefinition,
  IssueFieldType,
  PromptItem,
} from "../types";
import { Button, Field, Input, SectionHeader } from "../components/ui";

const FIELD_TYPES: Array<{ value: IssueFieldType; label: string }> = [
  { value: "text", label: "单行文本" },
  { value: "long_text", label: "多行文本" },
  { value: "single_select", label: "单选" },
  { value: "multiple_select", label: "多选" },
  { value: "boolean", label: "是 / 否" },
  { value: "number", label: "数字" },
  { value: "date", label: "日期" },
  { value: "datetime", label: "日期时间" },
  { value: "url", label: "链接" },
];

const RESERVED_FIELD_KEYS = new Set([
  "key",
  "values",
  "seed_message_id",
  "context_message_ids",
  "question_message_ids",
  "image_refs",
  "timeline",
  "tenant",
  "sender",
  "sender_id",
  "message_time",
  "raw_message_keys",
  "context_message_keys",
  "expected_image_count",
  "image_assignments",
  "image_status",
  "missing_image_names",
  "module_inference",
]);

const copyDefaultValue = (value: unknown): unknown => {
  if (Array.isArray(value)) return [...value];
  if (value && typeof value === "object") return { ...(value as Record<string, unknown>) };
  return value;
};

const cloneField = (field: IssueFieldDefinition): IssueFieldDefinition => ({
  ...field,
  options: [...field.options],
  default_value: copyDefaultValue(field.default_value),
});

const fallbackField = (): IssueFieldDefinition => ({
  key: "problem_description",
  label: "问题描述",
  type: "long_text",
  required: true,
  instruction: "简洁、客观地描述问题现象，保留必要业务上下文。",
  options: [],
  default_value: "",
});

const promptFields = (config: AppConfig): IssueFieldDefinition[] => {
  const configured = config.prompts.default_issue_fields?.length
    ? config.prompts.default_issue_fields
    : config.prompts.items.find((item) => item.issue_fields?.length)?.issue_fields;
  return (configured?.length ? configured : [fallbackField()]).map(cloneField);
};

const createPrompt = (config: AppConfig): PromptItem => ({
  id: `custom_${Date.now()}`,
  name: "新提示词",
  description: "说明这个提示词适合什么分析场景。",
  content: "请描述你希望大模型如何分析群聊；问题清单的字段约束会由系统自动附加。",
  issue_fields: promptFields(config),
  default_smart_sheet_template_id: "",
});

const fieldDefaultText = (field: IssueFieldDefinition) => {
  if (Array.isArray(field.default_value)) return field.default_value.join(", ");
  if (field.default_value === null || field.default_value === undefined) return "";
  return String(field.default_value);
};

function DefaultValueEditor({
  field,
  onChange,
}: {
  field: IssueFieldDefinition;
  onChange: (value: unknown) => void;
}) {
  if (field.type === "boolean") {
    return (
      <select
        className="input"
        value={field.default_value === true ? "true" : field.default_value === false ? "false" : ""}
        onChange={(event) => onChange(
          event.target.value === "" ? "" : event.target.value === "true",
        )}
      >
        <option value="">无默认值</option>
        <option value="true">是</option>
        <option value="false">否</option>
      </select>
    );
  }
  if (field.type === "multiple_select") {
    const selectedValues = Array.isArray(field.default_value)
      ? field.default_value.map(String)
      : [];
    return field.options.length ? (
      <div className="multi-default-options">
        {field.options.map((option) => (
          <label className="compact-check" key={option}>
            <input
              type="checkbox"
              checked={selectedValues.includes(option)}
              onChange={(event) => onChange(event.target.checked
                ? [...selectedValues, option]
                : selectedValues.filter((value) => value !== option))}
            />
            <span>{option}</span>
          </label>
        ))}
      </div>
    ) : <span className="empty-inline-hint">请先配置可选项</span>;
  }
  if (field.type === "single_select" && field.options.length) {
    return (
      <select className="input" value={fieldDefaultText(field)} onChange={(event) => onChange(event.target.value)}>
        <option value="">无默认值</option>
        {field.options.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    );
  }
  const inputType = field.type === "number"
    ? "number"
    : field.type === "date"
      ? "date"
      : field.type === "datetime"
        ? "datetime-local"
        : field.type === "url"
          ? "url"
          : "text";
  return (
    <Input
      type={inputType}
      value={fieldDefaultText(field)}
      placeholder="可留空"
      onChange={(event) => {
        const text = event.target.value;
        if (field.type === "number") {
          onChange(text === "" ? "" : Number(text));
        } else {
          onChange(text);
        }
      }}
    />
  );
}

const validatePrompts = (items: PromptItem[]): string | null => {
  for (const prompt of items) {
    if (!prompt.name.trim()) return "提示词名称不能为空";
    if (!prompt.issue_fields?.length) return `“${prompt.name}”至少需要一个问题字段`;
    const keys = new Set<string>();
    for (const field of prompt.issue_fields) {
      if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(field.key) || RESERVED_FIELD_KEYS.has(field.key)) {
        return `“${prompt.name}”中的字段 Key“${field.key || "（空）"}”无效或被系统占用`;
      }
      if (keys.has(field.key)) return `“${prompt.name}”中的字段 Key“${field.key}”重复`;
      keys.add(field.key);
      if (!field.label.trim()) return `字段“${field.key}”的显示名称不能为空`;
    }
  }
  return null;
};

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
  const [optionDrafts, setOptionDrafts] = useState<Record<string, string>>({});

  useEffect(() => {
    setDraft(config);
    setSelectedId((current) => config.prompts.items.some((item) => item.id === current)
      ? current
      : config.prompts.default_id);
    setOptionDrafts({});
  }, [config]);

  const selected = draft.prompts.items.find((item) => item.id === selectedId) ?? draft.prompts.items[0];
  const selectedFields = selected.issue_fields?.length ? selected.issue_fields : promptFields(draft);
  const duplicateKeys = useMemo(() => {
    const counts = new Map<string, number>();
    selectedFields.forEach((field) => counts.set(field.key, (counts.get(field.key) ?? 0) + 1));
    return new Set([...counts].filter(([, count]) => count > 1).map(([key]) => key));
  }, [selectedFields]);

  const updateSelected = (patch: Partial<PromptItem>) => {
    setDraft((previous) => ({
      ...previous,
      prompts: {
        ...previous.prompts,
        items: previous.prompts.items.map((item) => item.id === selected.id ? { ...item, ...patch } : item),
      },
    }));
  };

  const updateField = (index: number, patch: Partial<IssueFieldDefinition>) => {
    updateSelected({
      issue_fields: selectedFields.map((field, fieldIndex) => fieldIndex === index ? { ...field, ...patch } : field),
    });
  };

  const changeFieldType = (index: number, type: IssueFieldType) => {
    const field = selectedFields[index];
    let defaultValue = field.default_value;
    if (type === "boolean") defaultValue = typeof defaultValue === "boolean" ? defaultValue : "";
    else if (type === "multiple_select") defaultValue = [];
    else if (Array.isArray(defaultValue) || typeof defaultValue === "boolean") defaultValue = "";
    updateField(index, {
      type,
      options: type === "single_select" || type === "multiple_select" ? field.options : [],
      default_value: defaultValue,
    });
  };

  const addField = () => {
    let index = selectedFields.length + 1;
    let key = `custom_field_${index}`;
    const keys = new Set(selectedFields.map((field) => field.key));
    while (keys.has(key)) key = `custom_field_${++index}`;
    updateSelected({
      issue_fields: [...selectedFields, {
        key,
        label: "新字段",
        type: "text",
        required: false,
        instruction: "",
        options: [],
        default_value: "",
      }],
    });
  };

  const moveField = (index: number, offset: -1 | 1) => {
    const target = index + offset;
    if (target < 0 || target >= selectedFields.length) return;
    const next = [...selectedFields];
    [next[index], next[target]] = [next[target], next[index]];
    updateSelected({ issue_fields: next });
  };

  const removeField = (index: number) => {
    if (selectedFields.length <= 1) return;
    updateSelected({ issue_fields: selectedFields.filter((_, fieldIndex) => fieldIndex !== index) });
  };

  const addPrompt = () => {
    const prompt = createPrompt(draft);
    setDraft((previous) => ({ ...previous, prompts: { ...previous.prompts, items: [...previous.prompts.items, prompt] } }));
    setSelectedId(prompt.id);
  };

  const duplicatePrompt = () => {
    const prompt: PromptItem = {
      ...selected,
      id: `custom_${Date.now()}`,
      name: `${selected.name}（副本）`,
      issue_fields: selectedFields.map(cloneField),
    };
    setDraft((previous) => ({ ...previous, prompts: { ...previous.prompts, items: [...previous.prompts.items, prompt] } }));
    setSelectedId(prompt.id);
  };

  const removePrompt = () => {
    if (draft.prompts.items.length <= 1) return;
    const referencedSchedules = draft.schedules?.filter((schedule) => schedule.promptId === selected.id) ?? [];
    if (referencedSchedules.length) {
      toast.warning("该提示词正在被定时任务使用", {
        description: `请先修改定时任务：${referencedSchedules.map((schedule) => schedule.name).join("、")}`,
      });
      return;
    }
    if (!window.confirm(`确定删除提示词“${selected.name}”吗？`)) return;
    const remaining = draft.prompts.items.filter((item) => item.id !== selected.id);
    const nextDefault = draft.prompts.default_id === selected.id ? remaining[0].id : draft.prompts.default_id;
    setDraft((previous) => ({ ...previous, prompts: { ...previous.prompts, default_id: nextDefault, items: remaining } }));
    setSelectedId(remaining[0].id);
  };

  const save = async () => {
    const validationError = validatePrompts(draft.prompts.items);
    if (validationError) {
      toast.warning("请先修正问题清单", { description: validationError });
      return;
    }
    setSaving(true);
    try {
      await onSave(draft);
      toast.success("提示词和问题清单已保存");
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
        <div><div className="eyebrow"><MessageSquareText size={13} />Prompt Library</div><h1>提示词</h1><p>每套提示词可以定义自己的结构化问题清单，并关联默认腾讯文档模板。</p></div>
      </div>
      <div className="prompt-save-bar">
        <div>
          <strong>{selected.name || "当前提示词"}</strong>
          <span>{selectedFields.length} 个问题字段 · 修改后请保存配置</span>
        </div>
        <Button onClick={() => void save()} disabled={saving}><Save size={16} />{saving ? "保存中" : "保存更改"}</Button>
      </div>
      <div className="prompt-layout">
        <section className="glass-card prompt-list-panel">
          <SectionHeader title={`提示词库 · ${draft.prompts.items.length}`} action={<Button variant="ghost" onClick={addPrompt}><Plus size={16} /></Button>} />
          <div className="prompt-list">
            {draft.prompts.items.map((item) => (
              <button key={item.id} className={item.id === selected.id ? "prompt-list-item active" : "prompt-list-item"} onClick={() => setSelectedId(item.id)}>
                <span className="prompt-list-icon"><MessageSquareText size={17} /></span>
                <span><strong>{item.name}</strong><small>{item.issue_fields?.length ?? 0} 个问题字段 · {item.description || "暂无说明"}</small></span>
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
            <Field label="使用说明" hint="帮助使用者判断这套提示词的适用场景。"><Input value={selected.description} onChange={(event) => updateSelected({ description: event.target.value })} /></Field>
            <Field label="提示词内容" hint="系统会自动附加日期、群名、输出结构和下方字段约束。">
              <textarea className="textarea prompt-textarea" value={selected.content} onChange={(event) => updateSelected({ content: event.target.value })} />
            </Field>
            <Field label="默认腾讯文档模板" hint="运行任务时会自动带出，也可以临时更换。">
              <select className="input" value={selected.default_smart_sheet_template_id ?? ""} onChange={(event) => updateSelected({ default_smart_sheet_template_id: event.target.value })}>
                <option value="">跟随全局默认模板</option>
                {draft.smart_sheet.templates.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}
              </select>
            </Field>
          </div>

          <button
            className={selected.id === draft.prompts.default_id ? "default-selector selected" : "default-selector"}
            onClick={() => setDraft((previous) => ({ ...previous, prompts: { ...previous.prompts, default_id: selected.id } }))}
          >
            <span className="radio-mark">{selected.id === draft.prompts.default_id && <Check size={13} />}</span>
            <span><strong>设为默认分析提示词</strong><small>开始处理页面会自动使用它</small></span>
          </button>

          <div className="issue-schema-section">
            <div className="issue-schema-heading">
              <div><span className="section-kicker"><ListChecks size={14} />Issue fields</span><h3>问题清单字段</h3><p>字段顺序会用于模型输出、Excel 和 Markdown；Key 用于腾讯文档映射。</p></div>
              <Button variant="secondary" onClick={addField}><Plus size={15} />添加字段</Button>
            </div>
            <div className="issue-field-list">
              {selectedFields.map((field, index) => {
                const invalidKey = !/^[A-Za-z][A-Za-z0-9_]*$/.test(field.key) || RESERVED_FIELD_KEYS.has(field.key) || duplicateKeys.has(field.key);
                const supportsOptions = field.type === "single_select" || field.type === "multiple_select";
                const optionDraftKey = `${selected.id}:${field.key}`;
                return (
                  <article className={invalidKey ? "issue-field-card invalid" : "issue-field-card"} key={index}>
                    <div className="issue-field-toolbar">
                      <span className="field-order">{index + 1}</span>
                      <strong>{field.label || field.key || "未命名字段"}</strong>
                      <div className="inline-actions">
                        <Button variant="ghost" title="上移" disabled={index === 0} onClick={() => moveField(index, -1)}><ArrowUp size={14} /></Button>
                        <Button variant="ghost" title="下移" disabled={index === selectedFields.length - 1} onClick={() => moveField(index, 1)}><ArrowDown size={14} /></Button>
                        <Button variant="ghost" title="删除字段" disabled={selectedFields.length <= 1} onClick={() => removeField(index)}><Trash2 size={14} /></Button>
                      </div>
                    </div>
                    <div className="issue-field-grid">
                      <Field label="字段 Key" hint={invalidKey ? "需以字母开头，只能包含字母、数字和下划线，且不能重复。" : "保存后建议不要修改，以免影响历史映射。"}>
                        <Input className={invalidKey ? "input-invalid" : undefined} value={field.key} onChange={(event) => updateField(index, { key: event.target.value.trim() })} />
                      </Field>
                      <Field label="显示名称"><Input value={field.label} onChange={(event) => updateField(index, { label: event.target.value })} /></Field>
                      <Field label="字段类型">
                        <select className="input" value={field.type} onChange={(event) => changeFieldType(index, event.target.value as IssueFieldType)}>
                          {FIELD_TYPES.map((type) => <option key={type.value} value={type.value}>{type.label}</option>)}
                        </select>
                      </Field>
                      <label className="field field-required-toggle"><span className="field-label">必填</span><span className="compact-check"><input type="checkbox" checked={field.required} onChange={(event) => updateField(index, { required: event.target.checked })} /><span>模型必须给出值</span></span></label>
                      <Field label="提取说明" className="span-two" hint="告诉模型应该如何判断和填写这个字段。"><Input value={field.instruction} onChange={(event) => updateField(index, { instruction: event.target.value })} /></Field>
                      {supportsOptions && <Field label="可选项" hint="用逗号或换行分隔；模型只能输出这里定义的值。"><textarea className="textarea compact-textarea" value={optionDrafts[optionDraftKey] ?? field.options.join("\n")} onChange={(event) => {
                        const text = event.target.value;
                        setOptionDrafts((previous) => ({ ...previous, [optionDraftKey]: text }));
                        updateField(index, { options: text.split(/[,，\n]+/).map((item) => item.trim()).filter(Boolean) });
                      }} /></Field>}
                      <Field label="默认值" className={supportsOptions ? undefined : "span-two"}><DefaultValueEditor field={field} onChange={(default_value) => updateField(index, { default_value })} /></Field>
                    </div>
                  </article>
                );
              })}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

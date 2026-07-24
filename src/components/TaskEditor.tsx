import { useMemo, useState } from "react";
import { Check, MessagesSquare, RefreshCw, Search, X } from "lucide-react";
import type { AppConfig, GroupInfo, ProcessingOptions, TaskGroup } from "../types";
import { Button, Field, Switch } from "./ui";

const infoId = (group: GroupInfo) => String(group.conversation_id ?? group.id ?? "");
const infoName = (group: GroupInfo) =>
  String(group.display_name ?? group.name ?? infoId(group));

export function GroupMultiSelect({
  groups,
  selected,
  loading,
  onReload,
  onChange,
}: {
  groups: GroupInfo[];
  selected: TaskGroup[];
  loading: boolean;
  onReload: () => void;
  onChange: (groups: TaskGroup[]) => void;
}) {
  const [query, setQuery] = useState("");
  const selectedIds = useMemo(() => new Set(selected.map((group) => group.id)), [selected]);
  const filtered = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase();
    return groups.filter((group) => {
      const id = infoId(group);
      const name = infoName(group);
      return !keyword || name.toLocaleLowerCase().includes(keyword) || id.toLocaleLowerCase().includes(keyword);
    });
  }, [groups, query]);

  const toggle = (group: GroupInfo) => {
    const id = infoId(group);
    if (selectedIds.has(id)) {
      onChange(selected.filter((item) => item.id !== id));
      return;
    }
    onChange([...selected, { id, name: infoName(group) }]);
  };

  const selectFiltered = () => {
    const merged = new Map(selected.map((group) => [group.id, group]));
    filtered.forEach((group) => merged.set(infoId(group), { id: infoId(group), name: infoName(group) }));
    onChange([...merged.values()]);
  };

  return (
    <div className="group-picker">
      <div className="group-picker-toolbar">
        <div className="group-search">
          <Search size={14} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={groups.length ? "搜索群名称或 ID" : "读取后可搜索群聊"}
          />
        </div>
        <Button variant="secondary" onClick={onReload} disabled={loading}>
          <RefreshCw size={14} className={loading ? "spin" : ""} />
          {loading ? "读取中" : "读取群聊"}
        </Button>
      </div>

      {selected.length > 0 && (
        <div className="selected-groups">
          {selected.map((group) => (
            <span key={group.id} className="group-chip">
              {group.name}
              <button
                type="button"
                title={`移除 ${group.name}`}
                onClick={() => onChange(selected.filter((item) => item.id !== group.id))}
              >
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="group-list-header">
        <span>{groups.length ? `${filtered.length} 个可选群聊` : "尚未读取企业微信群"}</span>
        {filtered.length > 0 && (
          <button type="button" onClick={selectFiltered}>全选当前结果</button>
        )}
      </div>
      <div className="group-check-list">
        {filtered.length ? filtered.map((group) => {
          const id = infoId(group);
          const checked = selectedIds.has(id);
          return (
            <button
              type="button"
              key={id}
              className={checked ? "group-check-row selected" : "group-check-row"}
              onClick={() => toggle(group)}
            >
              <span className="group-avatar"><MessagesSquare size={14} /></span>
              <span><strong>{infoName(group)}</strong><small>{id}</small></span>
              <span className="check-box">{checked && <Check size={12} />}</span>
            </button>
          );
        }) : (
          <div className="group-list-empty">
            <MessagesSquare size={21} />
            <span>{groups.length ? "没有匹配的群聊" : "点击“读取群聊”从本地数据库加载"}</span>
          </div>
        )}
      </div>
    </div>
  );
}

export function ProcessingOptionsEditor({
  config,
  value,
  onChange,
  smartSheetHint = "完成后预览，确认才写入腾讯文档",
}: {
  config: AppConfig;
  value: ProcessingOptions;
  onChange: (value: ProcessingOptions) => void;
  smartSheetHint?: string;
}) {
  const update = <K extends keyof ProcessingOptions>(key: K, next: ProcessingOptions[K]) =>
    onChange({ ...value, [key]: next });
  const templates = config.smart_sheet.templates ?? [];
  const selectedPrompt = config.prompts.items.find((prompt) => prompt.id === value.promptId);
  const resolvedTemplateId = resolveSmartSheetTemplateId(config, value.promptId, value.smartSheetTemplateId);
  const updatePrompt = (promptId: string) => {
    onChange({
      ...value,
      promptId,
      smartSheetTemplateId: resolveSmartSheetTemplateId(config, promptId),
    });
  };
  return (
    <div className="task-options-editor">
      <Field label="分析提示词" hint="仅在启用大模型分析时使用">
        <select className="input" value={value.promptId} onChange={(event) => updatePrompt(event.target.value)}>
          {config.prompts.items.map((prompt) => (
            <option key={prompt.id} value={prompt.id}>{prompt.name}</option>
          ))}
        </select>
      </Field>
      <Field
        label="腾讯文档模板"
        hint={selectedPrompt?.default_smart_sheet_template_id
          ? `已关联“${selectedPrompt.name}”的默认模板；本次任务可以临时更换。`
          : "未单独关联时使用腾讯文档模板库中的全局默认项。"}
      >
        <select
          className="input"
          value={resolvedTemplateId}
          disabled={!templates.length}
          onChange={(event) => update("smartSheetTemplateId", event.target.value)}
        >
          {!templates.length && <option value="">尚未配置模板</option>}
          {templates.map((template) => (
            <option key={template.id} value={template.id}>
              {template.name}{template.id === config.smart_sheet.default_template_id ? "（全局默认）" : ""}
            </option>
          ))}
        </select>
      </Field>
      <div className="option-grid">
        <Switch checked={value.runOcr} onChange={(next) => update("runOcr", next)} label="截图 OCR" description="识别聊天截图中的文字" />
        <Switch checked={value.runAnalysis} onChange={(next) => update("runAnalysis", next)} label="大模型分析" description="合并同类问题并生成清单" />
        <Switch checked={value.exportXlsx} onChange={(next) => update("exportXlsx", next)} label="导出 Excel" description="业务筛选、补充和流转" />
        <Switch checked={value.exportMarkdown} onChange={(next) => update("exportMarkdown", next)} label="导出 Markdown" description="归档或交给其他 AI" />
        <Switch checked={value.prepareSmartSheet} onChange={(next) => update("prepareSmartSheet", next)} label="Smart Sheet" description={smartSheetHint} />
      </div>
    </div>
  );
}

export function resolveSmartSheetTemplateId(
  config: AppConfig,
  promptId: string,
  requestedTemplateId = "",
): string {
  const templates = config.smart_sheet.templates ?? [];
  const hasTemplate = (templateId: string | undefined) =>
    Boolean(templateId && templates.some((template) => template.id === templateId));
  if (hasTemplate(requestedTemplateId)) return requestedTemplateId;
  const prompt = config.prompts.items.find((item) => item.id === promptId);
  if (hasTemplate(prompt?.default_smart_sheet_template_id)) {
    return prompt?.default_smart_sheet_template_id ?? "";
  }
  if (hasTemplate(config.smart_sheet.default_template_id)) {
    return config.smart_sheet.default_template_id;
  }
  return templates[0]?.id ?? "";
}

export function defaultProcessingOptions(config: AppConfig): ProcessingOptions {
  const promptId = config.prompts.default_id;
  return {
    promptId,
    smartSheetTemplateId: resolveSmartSheetTemplateId(config, promptId),
    runOcr: Boolean(config.ocr.api_key && config.ocr.model),
    runAnalysis: Boolean(config.llm.api_key && config.llm.model),
    exportXlsx: true,
    exportMarkdown: true,
    prepareSmartSheet: false,
  };
}

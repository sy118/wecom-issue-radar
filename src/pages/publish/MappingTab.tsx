import { AlertTriangle, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button, Field, Input, SectionHeader } from "../../components/ui";
import {
  isRequiredSchemaTitle,
  stripRequiredSchemaTitle,
} from "../../lib/smartSheetSchema";
import {
  TRANSFORM_MODES,
  addMapping,
  enumMapText,
  invalidSourceIndexes,
  mappingDefaultText,
  parseEnumMap,
  parseMappingDefault,
  removeMapping,
  setTransform,
  updateMapping,
} from "./logic";
import { ProblemList } from "./ProblemList";
import { SchemaEditor } from "./SchemaEditor";
import type {
  LegacyTemplate,
  MappingProblem,
  PromptOption,
  PublishTransform,
  PublishTransformMode,
  SourceOption,
  TencentDraft,
} from "./types";

const transformForMode = (
  mode: PublishTransformMode,
  sourceKey: string,
  current: PublishTransform | undefined,
): PublishTransform => {
  const next: PublishTransform = { mode };
  if (mode === "constant") next.constant = current?.mode === "constant" ? current.constant ?? "" : "";
  if (mode === "template") {
    next.template = current?.mode === "template" && current.template
      ? current.template
      : `{{${sourceKey}}}`;
  }
  if (mode === "date") {
    next.date_format = current?.mode === "date" && current.date_format
      ? current.date_format
      : "YYYY-MM-DD";
  }
  if (mode === "enum") next.enum_map = current?.mode === "enum" ? current.enum_map ?? {} : {};
  return next;
};

export function MappingTab({
  draft,
  sources,
  prompts,
  problems,
  templates,
  onPromptChange,
  onChange,
}: {
  draft: TencentDraft;
  sources: SourceOption[];
  prompts: PromptOption[];
  problems: MappingProblem[];
  templates: LegacyTemplate[];
  onPromptChange: (promptId: string) => void;
  onChange: (draft: TencentDraft) => void;
}) {
  const invalidRows = new Set(invalidSourceIndexes(draft.field_mappings, sources));
  const selectedPrompt = prompts.find((prompt) => prompt.id === draft.prompt_id) ?? prompts[0];
  const sourceOptions = () => sources.map((source) => (
    <option key={source.key} value={source.key}>
      {source.label} · {source.key}
    </option>
  ));

  return (
    <div className="pub-tab-panel">
      <section className="pub-section">
        <SectionHeader
          title="提示词与来源字段"
          description="源字段来自问题分析提示词的问题字段；切换提示词会保留现有映射，失效的来源会标黄。"
        />
        <div className="form-grid two-columns">
          <Field label="问题分析提示词 / 策略" hint={`当前提供 ${selectedPrompt?.fields.length ?? 0} 个问题字段 + 5 个系统变量`}>
            <select
              className="input"
              value={draft.prompt_id}
              onChange={(event) => onPromptChange(event.target.value)}
            >
              {prompts.map((prompt) => (
                <option key={prompt.id || "default"} value={prompt.id}>
                  {prompt.name}{prompt.strategy ? "" : "（提示词）"}
                </option>
              ))}
            </select>
          </Field>
          <div className="pub-source-summary">
            {sources.map((source) => (
              <span key={source.key} className={source.system ? "pub-source-chip system" : "pub-source-chip"}>
                {source.label}
              </span>
            ))}
          </div>
        </div>
        {invalidRows.size > 0 && (
          <p className="pub-inline-warning">
            <AlertTriangle size={14} />
            有 {invalidRows.size} 条映射的来源字段不在当前提示词中；它们会被保留并标黄，切换回原提示词即可恢复。
          </p>
        )}
      </section>

      <section className="pub-section">
        <div className="pub-section-heading">
          <div>
            <h3>字段映射</h3>
            <p>每条映射把来源字段写入一个目标列；可配置转换方式、默认值与必填校验。</p>
          </div>
          <Button
            variant="secondary"
            onClick={() => {
              if (!Object.keys(draft.schema).length) {
                toast.warning("请先在下方定义目标字段 Schema");
                return;
              }
              const next = addMapping(draft, sources);
              if (next === draft) {
                toast.warning("所有目标字段都已有映射", { description: "可先删除重复映射或新增目标字段。" });
                return;
              }
              onChange(next);
            }}
          >
            <Plus size={15} /> 添加映射
          </Button>
        </div>

        <ProblemList problems={problems} />

        {draft.field_mappings.length ? (
          <div className="pub-mapping-list">
            {draft.field_mappings.map((mapping, index) => {
              const target = draft.schema[mapping.target_field_id];
              const targetType = typeof target?.type === "string" ? target.type : mapping.target_type || "text";
              const transform = draft.transforms[mapping.target_field_id] ?? { mode: "direct" as const };
              const targetRequired = isRequiredSchemaTitle(target?.title);
              const invalidSource = invalidRows.has(index);
              const targetOptions = Array.isArray(target?.enum) ? target.enum.map(String) : [];
              const changeMapping = (patch: Partial<typeof mapping>) => {
                let transforms = draft.transforms;
                if (patch.target_field_id && patch.target_field_id !== mapping.target_field_id) {
                  transforms = { ...transforms };
                  const existing = transforms[mapping.target_field_id];
                  if (existing) {
                    transforms[patch.target_field_id] = existing;
                    delete transforms[mapping.target_field_id];
                  }
                }
                onChange(updateMapping({ ...draft, transforms }, index, patch));
              };
              return (
                <div
                  key={`${mapping.target_field_id || "new"}-${index}`}
                  className={invalidSource ? "pub-mapping-card invalid-source" : "pub-mapping-card"}
                >
                  <div className="pub-mapping-top">
                    <Field label="来源字段">
                      <select
                        className="input"
                        value={mapping.source_key}
                        onChange={(event) => changeMapping({ source_key: event.target.value })}
                      >
                        {!sources.some((source) => source.key === mapping.source_key) && (
                          <option value={mapping.source_key}>{mapping.source_key || "请选择来源字段"}</option>
                        )}
                        {sourceOptions()}
                      </select>
                    </Field>
                    <Field label="目标字段">
                      <select
                        className="input"
                        value={mapping.target_field_id}
                        onChange={(event) => {
                          const targetFieldId = event.target.value;
                          const nextTarget = draft.schema[targetFieldId];
                          changeMapping({
                            target_field_id: targetFieldId,
                            target_type: typeof nextTarget?.type === "string" ? nextTarget.type : targetType,
                            required: isRequiredSchemaTitle(nextTarget?.title) || mapping.required,
                          });
                        }}
                      >
                        {!draft.schema[mapping.target_field_id] && (
                          <option value={mapping.target_field_id}>
                            {mapping.target_field_id || "请选择目标字段"}
                          </option>
                        )}
                        {Object.entries(draft.schema).map(([fieldId, field]) => {
                          const usedByOther = draft.field_mappings.some((item, itemIndex) => (
                            itemIndex !== index && item.target_field_id === fieldId
                          ));
                          return (
                            <option key={fieldId} value={fieldId} disabled={usedByOther}>
                              {isRequiredSchemaTitle(field.title) ? "*" : ""}
                              {stripRequiredSchemaTitle(typeof field.title === "string" ? field.title : fieldId) || fieldId} · {fieldId}
                            </option>
                          );
                        })}
                      </select>
                    </Field>
                    <Field label="转换方式">
                      <select
                        className="input"
                        value={transform.mode}
                        onChange={(event) => onChange(setTransform(
                          draft,
                          mapping.target_field_id,
                          transformForMode(event.target.value as PublishTransformMode, mapping.source_key, transform),
                        ))}
                      >
                        {TRANSFORM_MODES.map((mode) => (
                          <option key={mode.value} value={mode.value}>{mode.label}</option>
                        ))}
                      </select>
                    </Field>
                    <Button
                      variant="ghost"
                      title="删除映射"
                      onClick={() => onChange(removeMapping(draft, index))}
                    >
                      <Trash2 size={15} />
                    </Button>
                  </div>
                  <div className="pub-mapping-params">
                    {transform.mode === "constant" && (
                      <Field label="固定值">
                        <Input
                          value={transform.constant ?? ""}
                          placeholder="始终写入该值"
                          onChange={(event) => onChange(setTransform(draft, mapping.target_field_id, {
                            ...transform,
                            constant: event.target.value,
                          }))}
                        />
                      </Field>
                    )}
                    {transform.mode === "template" && (
                      <Field label="模板" hint="用 {{字段key}} 插入来源值，例如 {{problem_description}}（{{$date}}）">
                        <Input
                          value={transform.template ?? ""}
                          placeholder={`{{${mapping.source_key}}}`}
                          onChange={(event) => onChange(setTransform(draft, mapping.target_field_id, {
                            ...transform,
                            template: event.target.value,
                          }))}
                        />
                      </Field>
                    )}
                    {transform.mode === "date" && (
                      <Field label="日期格式" hint="支持 YYYY/YY/MM/DD/HH/mm/ss">
                        <Input
                          value={transform.date_format ?? "YYYY-MM-DD"}
                          placeholder="YYYY-MM-DD"
                          onChange={(event) => onChange(setTransform(draft, mapping.target_field_id, {
                            ...transform,
                            date_format: event.target.value,
                          }))}
                        />
                      </Field>
                    )}
                    {transform.mode === "enum" && (
                      <Field
                        label="枚举映射"
                        hint={targetOptions.length
                          ? `每行“源值=目标值”，目标选项：${targetOptions.join(" / ")}`
                          : "每行“源值=目标值”"}
                      >
                        <textarea
                          className="textarea pub-enum-textarea"
                          value={enumMapText(transform.enum_map)}
                          placeholder={"待处理=待处理\n已解决=已完成"}
                          onChange={(event) => onChange(setTransform(draft, mapping.target_field_id, {
                            ...transform,
                            enum_map: parseEnumMap(event.target.value),
                          }))}
                        />
                      </Field>
                    )}
                    {transform.mode === "direct" && (
                      <p className="pub-transform-hint">来源值原样写入；为空时使用默认值。</p>
                    )}
                    <Field label="默认值" hint="来源为空时回填；多选用逗号分隔">
                      <Input
                        value={mappingDefaultText(mapping.default_value)}
                        onChange={(event) => changeMapping({
                          default_value: parseMappingDefault(event.target.value, targetType),
                        })}
                      />
                    </Field>
                    <label className="pub-required">
                      <input
                        type="checkbox"
                        checked={mapping.required || targetRequired}
                        disabled={targetRequired}
                        title={targetRequired ? "目标 Schema 已标记为必填" : undefined}
                        onChange={(event) => changeMapping({ required: event.target.checked })}
                      />
                      <span>必填</span>
                    </label>
                  </div>
                  {invalidSource && (
                    <p className="pub-warn-badge">
                      <AlertTriangle size={13} />
                      来源字段“{mapping.source_key}”已失效：当前提示词没有该字段。映射会保留，可改选来源或切回原提示词。
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <div className="mapping-empty">还没有字段映射。先定义目标字段 Schema，再点击“添加映射”。</div>
        )}
      </section>

      <SchemaEditor draft={draft} templates={templates} onChange={onChange} />
    </div>
  );
}

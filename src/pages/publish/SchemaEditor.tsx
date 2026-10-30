import { useEffect, useState } from "react";
import { Copy, FileJson, Plus, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";
import { Button, Field, Input } from "../../components/ui";
import {
  isRequiredSchemaTitle,
  stripRequiredSchemaTitle,
  withRequiredSchemaTitle,
} from "../../lib/smartSheetSchema";
import {
  SMART_SHEET_TYPE_OPTIONS,
  addSchemaField,
  applySchema,
  importLegacyTemplate,
  parseSchemaJson,
  removeSchemaField,
  renameSchemaField,
  updateSchemaField,
  updateSchemaFieldRequired,
} from "./logic";
import type { LegacyTemplate, TencentDraft } from "./types";
import { copyToClipboard } from "../../lib/clipboard";
import { exportSchemaAsGenericJson, exportSchemaAsTencentFields } from "./schemaImport";
import { SchemaPasteImport } from "./SchemaPasteImport";

function SchemaRow({
  draft,
  fieldId,
  onChange,
}: {
  draft: TencentDraft;
  fieldId: string;
  onChange: (draft: TencentDraft) => void;
}) {
  const field = draft.schema[fieldId] ?? {};
  const title = typeof field.title === "string" ? field.title : "";
  const type = typeof field.type === "string" ? field.type : "text";
  const options = Array.isArray(field.enum) ? field.enum.map(String) : [];
  const required = isRequiredSchemaTitle(title);
  const selectType = type === "single_select" || type === "multiple_select";
  const typeOptions = SMART_SHEET_TYPE_OPTIONS.some((option) => option.value === type)
    ? SMART_SHEET_TYPE_OPTIONS
    : [...SMART_SHEET_TYPE_OPTIONS, { value: type, label: `${type}（自定义）` }];

  const [idDraft, setIdDraft] = useState(fieldId);
  const [idError, setIdError] = useState("");
  useEffect(() => {
    setIdDraft(fieldId);
    setIdError("");
  }, [fieldId]);

  const updateField = (patch: Partial<{ title: string; type: string; enum: string[] }>) => {
    onChange(applySchema(draft, updateSchemaField(draft.schema, fieldId, patch)));
  };

  const commitId = () => {
    const nextId = idDraft.trim();
    if (nextId === fieldId) {
      setIdError("");
      return;
    }
    const next = renameSchemaField(draft, fieldId, nextId);
    if (!next) {
      setIdError("字段 ID 为空或重复");
      setIdDraft(fieldId);
      return;
    }
    setIdError("");
    onChange(next);
  };

  return (
    <div className="pub-schema-row">
      <div className="pub-schema-cell pub-schema-id" data-label="字段 ID">
        <Input
          value={idDraft}
          aria-label="字段 ID"
          onChange={(event) => {
            setIdDraft(event.target.value);
            setIdError("");
          }}
          onBlur={commitId}
          onKeyDown={(event) => {
            if (event.key === "Enter") commitId();
          }}
        />
        {idError && <span className="pub-schema-error">{idError}</span>}
      </div>
      <div className="pub-schema-cell" data-label="列名称">
        <Input
          value={stripRequiredSchemaTitle(title)}
          placeholder="列名称"
          aria-label="字段名称"
          onChange={(event) => updateField({
            title: withRequiredSchemaTitle(event.target.value, required),
          })}
        />
      </div>
      <div className="pub-schema-cell" data-label="类型">
        <select
          className="input"
          value={type}
          aria-label="字段类型"
          onChange={(event) => updateField({ type: event.target.value })}
        >
          {typeOptions.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </div>
      <div className="pub-schema-cell" data-label="枚举选项">
        <Input
          value={options.join(", ")}
          placeholder={selectType ? "选项，用逗号分隔" : "仅选择类型需要选项"}
          aria-label="枚举选项"
          disabled={!selectType}
          onChange={(event) => updateField({
            enum: event.target.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
          })}
        />
      </div>
      <div className="pub-schema-cell pub-schema-required-cell" data-label="必填">
        <label className="pub-schema-required">
          <input
            type="checkbox"
            checked={required}
            onChange={(event) => onChange(applySchema(
              draft,
              updateSchemaFieldRequired(draft.schema, fieldId, event.target.checked),
            ))}
          />
          <span>必填</span>
        </label>
      </div>
      <div className="pub-schema-cell pub-schema-actions">
        <Button
          variant="ghost"
          title="删除字段"
          aria-label={`删除字段：${stripRequiredSchemaTitle(title) || fieldId}`}
          onClick={() => onChange(removeSchemaField(draft, fieldId))}
        >
          <Trash2 size={15} />
        </Button>
      </div>
    </div>
  );
}

export function SchemaEditor({
  draft,
  templates,
  onChange,
}: {
  draft: TencentDraft;
  templates: LegacyTemplate[];
  onChange: (draft: TencentDraft) => void;
}) {
  const [templateId, setTemplateId] = useState(templates[0]?.id ?? "");
  const [jsonText, setJsonText] = useState("");
  const [jsonError, setJsonError] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);

  useEffect(() => {
    if (advancedOpen) setJsonText(JSON.stringify(draft.schema, null, 2));
  }, [advancedOpen, draft.schema]);

  const importTemplate = () => {
    const template = templates.find((item) => item.id === templateId);
    if (!template) return;
    onChange(importLegacyTemplate(draft, template));
  };

  const copySchema = async (kind: "generic" | "tencent") => {
    const content = kind === "generic"
      ? exportSchemaAsGenericJson(draft.schema)
      : exportSchemaAsTencentFields(draft.schema);
    try {
      await copyToClipboard(content);
      toast.success(kind === "generic" ? "已复制通用 JSON" : "已复制腾讯字段数组");
    } catch {
      toast.error("复制失败", { description: "请检查系统剪贴板权限后重试。" });
    }
  };

  const applyJson = () => {
    try {
      onChange(applySchema(draft, parseSchemaJson(jsonText)));
      setJsonError("");
      setAdvancedOpen(false);
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : "Schema 无效");
    }
  };

  return (
    <section className="pub-section">
      <div className="pub-section-heading">
        <div>
          <h3>目标字段 Schema</h3>
          <p>字段 ID 来自腾讯文档列；名称前带 * 表示必填。增删列只影响预览与映射，不会修改远端文档。</p>
        </div>
        <div className="inline-actions">
          <Button
            variant="ghost"
            title="复制当前 Schema 为通用 JSON"
            onClick={() => void copySchema("generic")}
          >
            <Copy size={15} /> 复制通用 JSON
          </Button>
          <Button
            variant="ghost"
            title="复制当前 Schema 为腾讯字段数组"
            onClick={() => void copySchema("tencent")}
          >
            <Copy size={15} /> 复制腾讯字段数组
          </Button>
          <Button
            variant="secondary"
            onClick={() => onChange(applySchema(draft, addSchemaField(draft.schema).schema))}
          >
            <Plus size={15} /> 添加字段
          </Button>
        </div>
      </div>

      {Object.keys(draft.schema).length ? (
        <>
          <div className="pub-schema-head">
            <span>字段 ID</span>
            <span>列名称</span>
            <span>类型</span>
            <span>枚举选项</span>
            <span>必填</span>
            <span />
          </div>
          <div className="pub-schema-table">
            {Object.keys(draft.schema).map((fieldId) => (
              <SchemaRow
                key={fieldId}
                draft={draft}
                fieldId={fieldId}
                onChange={onChange}
              />
            ))}
          </div>
        </>
      ) : (
        <div className="mapping-empty">还没有目标字段。可以手动添加，或从旧版腾讯文档模板导入。</div>
      )}

      <SchemaPasteImport onApply={(schema) => onChange(applySchema(draft, schema))} />

      {templates.length > 0 && (
        <div className="pub-import-row">
          <Field label="从旧版模板导入" hint="会用所选模板的 Schema 与字段映射覆盖当前目标">
            <select
              className="input"
              value={templateId}
              onChange={(event) => setTemplateId(event.target.value)}
            >
              {templates.map((template) => (
                <option key={template.id} value={template.id}>
                  {template.name}（{Object.keys(template.schema ?? {}).length} 字段 / {template.field_mappings?.length ?? 0} 映射）
                </option>
              ))}
            </select>
          </Field>
          <Button variant="secondary" onClick={importTemplate}>
            <Upload size={15} /> 导入
          </Button>
        </div>
      )}

      <details
        className="pub-advanced"
        open={advancedOpen}
        onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
      >
        <summary><FileJson size={15} /> 高级：直接编辑 Schema JSON</summary>
        <textarea
          className={jsonError ? "textarea pub-json-textarea input-invalid" : "textarea pub-json-textarea"}
          spellCheck={false}
          value={jsonText}
          onChange={(event) => {
            setJsonText(event.target.value);
            if (jsonError) setJsonError("");
          }}
        />
        {jsonError && <p className="schema-error">{jsonError}</p>}
        <div className="pub-adv-actions">
          <p>应用后会保留仍存在的字段映射，失效映射会被移除。</p>
          <Button variant="secondary" onClick={applyJson}>应用 JSON</Button>
        </div>
      </details>
    </section>
  );
}

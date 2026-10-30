import { useState } from "react";
import { Check, ClipboardPaste, Eraser } from "lucide-react";
import { toast } from "sonner";
import type { SmartSheetFieldSchema } from "../../types";
import { Button } from "../../components/ui";
import { stripRequiredSchemaTitle } from "../../lib/smartSheetSchema";
import { ProblemList } from "./ProblemList";
import { parseTencentSchema, schemaTypeLabel, type SchemaImportResult } from "./schemaImport";
import type { MappingProblem } from "./types";

const PLACEHOLDER = `{
  "fields": [
    { "field_id": "f1", "field_title": "*问题描述", "field_type": "text" },
    { "field_id": "f2", "field_title": "影响范围", "field_type": "单选",
      "options": ["单个客户", "多个客户"] }
  ]
}`;

export function SchemaPasteImport({
  onApply,
}: {
  onApply: (schema: Record<string, SmartSheetFieldSchema>) => void;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [result, setResult] = useState<SchemaImportResult | null>(null);
  const [error, setError] = useState("");

  const update = (value: string) => {
    setText(value);
    if (!value.trim()) {
      setResult(null);
      setError("");
      return;
    }
    try {
      setResult(parseTencentSchema(value));
      setError("");
    } catch (reason) {
      setResult(null);
      setError(reason instanceof Error ? reason.message : "Schema 转换失败");
    }
  };

  const reset = () => {
    setText("");
    setResult(null);
    setError("");
  };

  const apply = () => {
    if (!result) return;
    onApply(result.schema);
    toast.success(`已导入 ${result.fields.length} 个字段`, {
      description: "保存发布目标后生效；请回到字段映射核对来源与目标列。",
    });
    reset();
    setOpen(false);
  };

  const problems: MappingProblem[] = result
    ? result.problems.map((problem, index) => ({
      key: `import:${index}`,
      level: problem.level,
      message: problem.message,
    }))
    : [];

  return (
    <details
      className="pub-advanced pub-tencent-import"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary><ClipboardPaste size={15} /> 从腾讯文档粘贴 Schema（自动转换）</summary>
      <div className="pub-import-body">
        <p className="pub-import-hint">
          支持内部对象 <code>{`{字段ID:{title,type}}`}</code>、字段数组
          <code>[{`{field_id,field_title,field_type}`}]</code>，以及
          <code>fields / schema / columns / data</code> 包装；腾讯常见类型与 options/required 会自动归一。
          先预览，确认后再写入（会覆盖当前 Schema，并保留仍存在的字段映射）。
        </p>
        <textarea
          className={error ? "textarea pub-import-textarea input-invalid" : "textarea pub-import-textarea"}
          spellCheck={false}
          value={text}
          placeholder={PLACEHOLDER}
          onChange={(event) => update(event.target.value)}
        />
        {error && <p className="schema-error">{error}</p>}

        {result && (
          <>
            <div className="pub-import-summary">
              <strong>识别到 {result.fields.length} 个字段</strong>
              {Object.entries(result.typeCounts).map(([type, count]) => (
                <span key={type} className="pub-type-chip">{schemaTypeLabel(type)} × {count}</span>
              ))}
            </div>
            <ProblemList problems={problems} />
            {result.notes.length > 0 && (
              <div className="pub-import-notes">
                {result.notes.map((note) => <p key={note}>{note}</p>)}
              </div>
            )}
            <div
              className="pub-import-scroll"
              tabIndex={0}
              role="region"
              aria-label="识别结果预览（可用方向键滚动）"
            >
              <table className="pub-import-table" aria-label="识别到的字段">
                <thead>
                  <tr>
                    <th>字段 ID</th>
                    <th>列名称</th>
                    <th>类型</th>
                    <th>选项</th>
                    <th>必填</th>
                    <th>原类型</th>
                  </tr>
                </thead>
                <tbody>
                  {result.fields.map((field) => (
                    <tr key={field.fieldId}>
                      <td><code>{field.fieldId}</code></td>
                      <td>{stripRequiredSchemaTitle(field.title) || field.title}</td>
                      <td>{schemaTypeLabel(field.type)}</td>
                      <td>{field.options.length ? field.options.join("，") : "—"}</td>
                      <td>{field.required ? "是" : "—"}</td>
                      <td><code>{field.originalType || "—"}</code></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="pub-import-actions">
              <Button variant="ghost" onClick={reset}><Eraser size={15} /> 清空</Button>
              <Button onClick={apply}><Check size={15} /> 确认写入 {result.fields.length} 个字段</Button>
            </div>
          </>
        )}
      </div>
    </details>
  );
}

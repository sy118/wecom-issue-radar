import { CheckCircle2, CircleAlert, Eye } from "lucide-react";
import { SectionHeader } from "../../components/ui";
import { ProblemList } from "./ProblemList";
import type { PreviewResult } from "./types";

const MODE_LABEL: Record<string, string> = {
  direct: "直接使用",
  constant: "固定值",
  template: "模板拼接",
  date: "日期格式化",
  enum: "枚举映射",
};

export function PreviewTab({
  preview,
  promptName,
}: {
  preview: PreviewResult;
  promptName: string;
}) {
  const errors = preview.problems.filter((problem) => problem.level === "error").length;
  const warnings = preview.problems.length - errors;
  return (
    <div className="pub-tab-panel">
      <section className="pub-section">
        <SectionHeader
          title="预览"
          description={`用示例问题渲染每一列的最终值；来源字段来自“${promptName}”与系统变量，不会读取真实群聊。`}
          action={
            <span className={errors ? "pub-preview-status error" : "pub-preview-status"}>
              {errors ? <CircleAlert size={14} /> : <CheckCircle2 size={14} />}
              {preview.rows.length} 列 · {errors} 个错误 · {warnings} 个提示
            </span>
          }
        />
        <div className="pub-source-grid">
          {preview.sample.map((item) => (
            <div key={item.key} className="pub-source-card">
              <span>{item.label}<code>{item.key}</code></span>
              <strong>{item.display || "（空）"}</strong>
            </div>
          ))}
        </div>
      </section>

      <section className="pub-section">
        <div className="pub-section-heading">
          <div>
            <h3>最终写入值</h3>
            <p>未映射的列会留空；必填列会显示为错误。</p>
          </div>
          <Eye size={17} className="pub-muted-icon" />
        </div>
        {preview.rows.length ? (
          <div className="pub-preview-scroll">
            <table className="pub-preview-table">
              <thead>
                <tr>
                  <th>目标字段</th>
                  <th>类型</th>
                  <th>来源字段</th>
                  <th>转换</th>
                  <th>最终值</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((row) => {
                  const status = !row.mapped
                    ? { label: "未映射", className: "warn" }
                    : row.sourceMissing
                      ? { label: "来源失效", className: "warn" }
                      : { label: "正常", className: "ok" };
                  return (
                    <tr key={row.fieldId}>
                      <td>
                        <strong>{row.title}</strong>
                        <code>{row.fieldId}</code>
                      </td>
                      <td>{row.type}</td>
                      <td><code>{row.sourceKey || "—"}</code></td>
                      <td>{row.mapped ? MODE_LABEL[row.mode] ?? row.mode : "—"}</td>
                      <td className="pub-preview-value">{row.display || "（空）"}</td>
                      <td><span className={`pub-status ${status.className}`}>{status.label}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="mapping-empty">还没有目标字段，先在“字段映射”中定义 Schema。</div>
        )}
        <ProblemList problems={preview.problems} />
      </section>
    </div>
  );
}

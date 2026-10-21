import { useState } from "react";
import {
  AlertCircle,
  CalendarRange,
  CheckCircle2,
  Download,
  FileJson2,
  FileText,
  FolderOpen,
  Image as ImageIcon,
  LoaderCircle,
  X,
} from "lucide-react";
import clsx from "clsx";

import { Field, Input } from "../ui";
import { formatFileSizeLabel } from "../../pages/wechat/logic";
import type {
  WechatExportResult,
  WechatProject,
} from "../../pages/wechat/types";

export interface ExportDialogProps {
  projects: WechatProject[];
  initialProjectId: string;
  exporting: boolean;
  onClose: () => void;
  onExport: (projectId: string, start?: string, end?: string) => Promise<WechatExportResult>;
  onOpenPath: (path: string) => void;
}

export function ExportDialog({
  projects,
  initialProjectId,
  exporting,
  onClose,
  onExport,
  onOpenPath,
}: ExportDialogProps) {
  const [projectId, setProjectId] = useState(
    initialProjectId || projects[0]?.id || "",
  );
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<WechatExportResult | null>(null);

  const submit = async () => {
    if (!projectId) {
      setError("请选择一个会话项目。");
      return;
    }
    if (start && end && start > end) {
      setError("开始时间不能晚于结束时间。");
      return;
    }
    setError("");
    try {
      const exported = await onExport(
        projectId,
        start ? toBackendTime(start) : undefined,
        end ? toBackendTime(end) : undefined,
      );
      setResult(exported);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "导出失败，请稍后重试。");
    }
  };

  return (
    <div className="modal-backdrop">
      <section className="wechat-drawer wechat-export-drawer" role="dialog" aria-modal="true" aria-label="导出个人微信会话">
        <header className="wechat-drawer-header">
          <div>
            <span className="eyebrow">PORTABLE ARCHIVE</span>
            <h2>导出个人微信会话</h2>
          </div>
          <button type="button" className="wechat-icon-button" aria-label="关闭" onClick={onClose}>
            <X size={17} />
          </button>
        </header>

        <div className="wechat-drawer-body">
          {result ? (
            <div className="wechat-export-result">
              <div className={clsx("wechat-export-seal", result.dir ? "is-good" : "is-warn")}>
                {result.dir ? <CheckCircle2 size={26} /> : <AlertCircle size={26} />}
              </div>
              <h3>{result.dir ? "导出完成" : "导出已执行，但没有返回目录"}</h3>
              <p className="wechat-export-dir">{result.dir || "请检查后端日志确认导出结果。"}</p>
              {result.files.length > 0 && (
                <div className="wechat-export-files">
                  {result.files.map((file) => (
                    <span key={file}>
                      {formatFileSizeLabel(file) === "JSON"
                        ? <FileJson2 size={14} />
                        : formatFileSizeLabel(file) === "MD"
                          ? <FileText size={14} />
                          : <ImageIcon size={14} />}
                      <small title={file}>{file.split(/[\\/]/).pop() || file}</small>
                      <em>{formatFileSizeLabel(file)}</em>
                    </span>
                  ))}
                </div>
              )}
              <div className="wechat-drawer-footer wechat-export-footer">
                <button
                  type="button"
                  className="button button-secondary"
                  disabled={!result.dir}
                  onClick={() => onOpenPath(result.dir)}
                >
                  <FolderOpen size={15} />打开结果目录
                </button>
                <button type="button" className="button button-primary" onClick={onClose}>完成</button>
              </div>
            </div>
          ) : (
            <div className="wechat-export-form">
              <div className="wechat-export-intro">
                <FolderOpen size={24} />
                <span>
                  <strong>生成可回读的聊天归档</strong>
                  <small>一次导出包含 Markdown、结构化 JSON 与独立媒体目录；不可用媒体保留原因。</small>
                </span>
              </div>

              <Field label="会话项目">
                <select
                  className="input"
                  value={projectId}
                  onChange={(event) => setProjectId(event.target.value)}
                >
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name} · {project.kind === "group" ? "群聊" : "单聊"}
                    </option>
                  ))}
                </select>
              </Field>

              <div className="form-grid two-columns">
                <Field label="开始时间" hint="留空表示从第一条记录开始。">
                  <Input type="datetime-local" value={start} onChange={(event) => setStart(event.target.value)} />
                </Field>
                <Field label="结束时间" hint="留空表示导出到最新记录。">
                  <Input type="datetime-local" value={end} onChange={(event) => setEnd(event.target.value)} />
                </Field>
              </div>

              <div className="wechat-export-formats">
                <span><FileText size={15} /><strong>chat.md</strong><small>适合阅读</small></span>
                <span><FileJson2 size={15} /><strong>chat.json</strong><small>适合回读</small></span>
                <span><CalendarRange size={15} /><strong>{start || end ? "指定范围" : "全部时间"}</strong><small>媒体单独成目录</small></span>
              </div>

              {error && (
                <div className="wechat-inline-error"><AlertCircle size={14} /><span>{error}</span></div>
              )}

              <div className="wechat-drawer-footer wechat-export-footer">
                <span className="wechat-export-hint">导出只读取本地数据，不会修改微信原始数据库。</span>
                <button type="button" className="button button-secondary" onClick={onClose}>取消</button>
                <button
                  type="button"
                  className="button button-primary"
                  disabled={exporting || !projectId}
                  onClick={() => void submit()}
                >
                  {exporting ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}
                  {exporting ? "正在导出" : "开始导出"}
                </button>
              </div>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

/** `datetime-local` values are already ISO-like; send them as local dates. */
function toBackendTime(value: string): string {
  return value.length === 16 ? `${value}:00` : value;
}

import { useEffect, useMemo, useState } from "react";
import {
  Bot,
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  CloudUpload,
  ExternalLink,
  FileSpreadsheet,
  FileText,
  ImageIcon,
  LoaderCircle,
  MessagesSquare,
  Play,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import type { AppConfig, GroupInfo, SmartSheetPreview, TaskRequest, TaskResult } from "../types";
import { Button, Field, SectionHeader, Switch } from "../components/ui";

const today = () => {
  const date = new Date();
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
};

const groupId = (group: GroupInfo) => String(group.conversation_id ?? group.id ?? "");
const groupName = (group: GroupInfo) =>
  String(group.display_name ?? group.name ?? groupId(group));

export function RunPage({ config }: { config: AppConfig }) {
  const [date, setDate] = useState(today);
  const [groups, setGroups] = useState<GroupInfo[]>([]);
  const [selectedGroup, setSelectedGroup] = useState(config.target_group_id || "");
  const [loadingGroups, setLoadingGroups] = useState(false);
  const [running, setRunning] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [result, setResult] = useState<TaskResult | null>(null);
  const [preview, setPreview] = useState<SmartSheetPreview | null>(null);
  const [options, setOptions] = useState({
    runOcr: Boolean(config.ocr.api_key && config.ocr.model),
    runAnalysis: Boolean(config.llm.api_key && config.llm.model),
    exportXlsx: true,
    exportMarkdown: true,
    prepareSmartSheet: false,
  });

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void bridge.onProgress((message) => {
      if (!disposed) setLogs((previous) => [...previous, message]);
    }).then((cleanup) => {
      unlisten = cleanup;
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  const activeGroup = useMemo(
    () => groups.find((group) => groupId(group) === selectedGroup),
    [groups, selectedGroup],
  );

  const loadGroups = async () => {
    setLoadingGroups(true);
    try {
      const response = await bridge.listGroups();
      setGroups(response.groups);
      if (!selectedGroup && response.groups.length) {
        setSelectedGroup(groupId(response.groups[0]));
      }
      toast.success(`已读取 ${response.groups.length} 个群聊`);
    } catch (error) {
      toast.error(`读取群聊失败：${String(error)}`);
    } finally {
      setLoadingGroups(false);
    }
  };

  const execute = async () => {
    const id = selectedGroup || config.target_group_id;
    if (!id) {
      toast.warning("请先读取并选择一个企业微信群");
      return;
    }
    const name = activeGroup ? groupName(activeGroup) : config.target_group_name || id;
    const request: TaskRequest = {
      date,
      groupId: id,
      groupName: name,
      promptId: config.prompts.default_id,
      ...options,
    };
    setRunning(true);
    setResult(null);
    setLogs([`开始处理 ${date} · ${name}`]);
    try {
      const nextResult = await bridge.runTask(request);
      setResult(nextResult);
      setLogs((previous) => [...previous, "处理完成"]);
      if (nextResult.smartSheetPreview?.pending) {
        setPreview(nextResult.smartSheetPreview);
      }
      toast.success("聊天记录处理完成");
    } catch (error) {
      const message = String(error);
      setLogs((previous) => [...previous, `失败：${message}`]);
      toast.error(`处理失败：${message}`);
    } finally {
      setRunning(false);
    }
  };

  const confirmSync = async () => {
    if (!result) return;
    setSyncing(true);
    try {
      const response = await bridge.syncSmartSheet(result.dayDir, date);
      toast.success(`已写入腾讯文档 Smart Sheet：${response.synced ?? preview?.pending ?? 0} 条`);
      setPreview(null);
    } catch (error) {
      toast.error(`同步失败：${String(error)}`);
    } finally {
      setSyncing(false);
    }
  };

  const updateOption = (key: keyof typeof options, value: boolean) =>
    setOptions((previous) => ({ ...previous, [key]: value }));

  return (
    <div className="page-content">
      <div className="page-title-row">
        <div>
          <div className="eyebrow"><Sparkles size={13} />AI 工作流</div>
          <h1>开始处理</h1>
          <p>提取指定日期的群聊，生成可追溯的问题清单和业务报表。</p>
        </div>
        <div className="ready-badge"><span className="status-dot" />本机数据，本地处理</div>
      </div>

      <div className="run-layout">
        <div className="run-primary">
          <section className="glass-card">
            <SectionHeader
              title="处理范围"
              description="日期默认今天，群聊来自企业微信本地数据库。"
              action={
                <Button variant="secondary" onClick={() => void loadGroups()} disabled={loadingGroups}>
                  <RefreshCw size={15} className={loadingGroups ? "spin" : ""} />
                  {loadingGroups ? "读取中" : "读取群聊"}
                </Button>
              }
            />
            <div className="form-grid two-columns">
              <Field label="处理日期">
                <div className="input-with-icon">
                  <CalendarDays size={16} />
                  <input type="date" value={date} onChange={(event) => setDate(event.target.value)} />
                </div>
              </Field>
              <Field label="企业微信群" hint={groups.length ? `已发现 ${groups.length} 个群聊` : "点击右上角读取群聊列表"}>
                <div className="input-with-icon">
                  <MessagesSquare size={16} />
                  <select value={selectedGroup} onChange={(event) => setSelectedGroup(event.target.value)}>
                    {!groups.length && (
                      <option value={config.target_group_id || ""}>
                        {config.target_group_name || "尚未读取群聊"}
                      </option>
                    )}
                    {groups.map((group) => (
                      <option key={groupId(group)} value={groupId(group)}>{groupName(group)}</option>
                    ))}
                  </select>
                </div>
              </Field>
            </div>
          </section>

          <section className="glass-card">
            <SectionHeader title="处理能力" description="可以只导出原始聊天，也可以同时使用 OCR 和大模型分析。" />
            <div className="option-grid">
              <Switch checked={options.runOcr} onChange={(value) => updateOption("runOcr", value)} label="截图 OCR" description="识别聊天截图中的文字" />
              <Switch checked={options.runAnalysis} onChange={(value) => updateOption("runAnalysis", value)} label="大模型分析" description="按当前默认提示词提炼问题" />
              <Switch checked={options.exportXlsx} onChange={(value) => updateOption("exportXlsx", value)} label="导出 Excel" description="聊天记录、OCR 和问题清单" />
              <Switch checked={options.exportMarkdown} onChange={(value) => updateOption("exportMarkdown", value)} label="导出 Markdown" description="适合归档或交给其他 AI" />
              <Switch checked={options.prepareSmartSheet} onChange={(value) => updateOption("prepareSmartSheet", value)} label="同步 Smart Sheet" description="完成后预览，确认才写入腾讯文档" />
            </div>
          </section>

          <section className="glass-card prompt-summary">
            <div className="prompt-icon"><Bot size={21} /></div>
            <div>
              <span className="field-label">本次分析提示词</span>
              <strong>{config.prompts.items.find((item) => item.id === config.prompts.default_id)?.name ?? "标准问题盘点"}</strong>
              <p>{config.prompts.items.find((item) => item.id === config.prompts.default_id)?.description}</p>
            </div>
            <ChevronRight size={17} />
          </section>

          <Button className="run-button" disabled={running} onClick={() => void execute()}>
            {running ? <LoaderCircle className="spin" size={18} /> : <Play size={18} fill="currentColor" />}
            {running ? "正在处理，请稍候…" : "开始处理"}
          </Button>
        </div>

        <aside className="run-secondary">
          <section className="glass-card status-card">
            <SectionHeader title="运行状态" description={running ? "任务正在后台执行" : result ? "最近一次任务已完成" : "等待开始"} />
            <div className="progress-rail">
              <span className={running ? "progress-live" : result ? "progress-done" : ""} />
            </div>
            <div className="log-list">
              {logs.length ? logs.slice(-7).map((log, index) => (
                <div className="log-line" key={`${index}-${log}`}>
                  {index === logs.slice(-7).length - 1 && running ? <LoaderCircle className="spin" size={14} /> : <CheckCircle2 size={14} />}
                  <span>{log}</span>
                </div>
              )) : <div className="empty-log">处理阶段和结果会显示在这里</div>}
            </div>
          </section>

          <section className="glass-card output-card">
            <SectionHeader title="本次输出" />
            {result ? (
              <div className="output-list">
                {Object.entries(result.outputs).map(([kind, path]) => (
                  <button key={kind} onClick={() => void bridge.openPath(path)}>
                    <span className={`file-icon file-${kind}`}>
                      {kind === "xlsx" ? <FileSpreadsheet size={18} /> : <FileText size={18} />}
                    </span>
                    <span><strong>{kind === "xlsx" ? "Excel 工作簿" : "Markdown 归档"}</strong><small>{path}</small></span>
                    <ExternalLink size={14} />
                  </button>
                ))}
                <button onClick={() => void bridge.openPath(result.dayDir)}>
                  <span className="file-icon"><ImageIcon size={18} /></span>
                  <span><strong>完整任务目录</strong><small>原始记录、附件和中间结果</small></span>
                  <ExternalLink size={14} />
                </button>
              </div>
            ) : <div className="output-placeholder"><FileText size={28} /><span>任务完成后可从这里打开文件</span></div>}
          </section>
        </aside>
      </div>

      {preview && result && (
        <div className="modal-backdrop">
          <div className="modal-card">
            <div className="modal-icon"><CloudUpload size={24} /></div>
            <h2>确认写入腾讯文档？</h2>
            <p>即将向已配置的 Smart Sheet 写入 <strong>{preview.pending}</strong> 条新问题；{preview.already_synced} 条已同步记录会自动跳过。</p>
            <div className="modal-note">这是外部写入操作。取消后，本地 Excel 和 Markdown 仍已正常生成。</div>
            <div className="modal-actions">
              <Button variant="secondary" onClick={() => setPreview(null)}>暂不同步</Button>
              <Button disabled={syncing} onClick={() => void confirmSync()}>
                {syncing && <LoaderCircle className="spin" size={16} />}
                确认写入 {preview.pending} 条
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

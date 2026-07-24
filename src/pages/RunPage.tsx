import { useEffect, useMemo, useState } from "react";
import {
  CalendarDays,
  CheckCircle2,
  Clock3,
  CloudUpload,
  ExternalLink,
  FileSpreadsheet,
  FileText,
  FolderOpen,
  LoaderCircle,
  Play,
  Sparkles,
  UsersRound,
} from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import {
  createRunSession,
  loadRunSession,
  saveRunSession,
  validateRunRange,
  type RunSessionState,
} from "../lib/runSession";
import type {
  AppConfig,
  GroupInfo,
  ProcessingOptions,
  TaskGroup,
  TaskRequest,
  TaskResult,
  TaskRunResult,
} from "../types";
import { Button, Field, SectionHeader } from "../components/ui";
import {
  defaultProcessingOptions,
  GroupMultiSelect,
  ProcessingOptionsEditor,
} from "../components/TaskEditor";

const localDate = (offsetDays = 0) => {
  const date = new Date();
  date.setDate(date.getDate() + offsetDays);
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
};

const browserStorage = () => {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

const initialGroups = (config: AppConfig): TaskGroup[] => {
  if (!config.target_group_id || config.target_group_id.includes("<")) return [];
  return [{
    id: config.target_group_id,
    name: config.target_group_name || config.target_group_id,
  }];
};

function normalizeRuns(result: TaskResult | null, fallbackGroups: TaskGroup[]): TaskRunResult[] {
  if (!result) return [];
  if (result.runs?.length) return result.runs;
  if (!result.dayDir || !result.outputs) return [];
  return [{
    groupId: fallbackGroups[0]?.id ?? "",
    groupName: fallbackGroups[0]?.name ?? "群聊",
    dayDir: result.dayDir,
    outputs: result.outputs,
    definitionPath: result.definitionPath,
    smartSheetPreview: result.smartSheetPreview,
  }];
}

export function RunPage({ config }: { config: AppConfig }) {
  const today = localDate();
  const [initialSession] = useState<RunSessionState>(() => {
    const fallback = createRunSession(today, initialGroups(config), defaultProcessingOptions(config));
    const storage = browserStorage();
    if (!storage) return fallback;
    const restored = loadRunSession(storage);
    if (!restored) return fallback;
    if (restored.endDate > today) {
      return {
        ...restored,
        startDate: today,
        endDate: today,
        startTime: "00:00",
        endTime: "23:59",
      };
    }
    return restored;
  });
  const [startDate, setStartDate] = useState(initialSession.startDate);
  const [endDate, setEndDate] = useState(initialSession.endDate);
  const [startTime, setStartTime] = useState(initialSession.startTime);
  const [endTime, setEndTime] = useState(initialSession.endTime);
  const [groups, setGroups] = useState<GroupInfo[]>([]);
  const [selectedGroups, setSelectedGroups] = useState<TaskGroup[]>(initialSession.selectedGroups);
  const [options, setOptions] = useState<ProcessingOptions>(initialSession.options);
  const [loadingGroups, setLoadingGroups] = useState(false);
  const [running, setRunning] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [logs, setLogs] = useState<string[]>(initialSession.logs);
  const [result, setResult] = useState<TaskResult | null>(initialSession.result);
  const [confirmingSync, setConfirmingSync] = useState(false);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void bridge.onProgress((message) => {
      if (!disposed) setLogs((previous) => [...previous, message]);
    }).then((cleanup) => {
      unlisten = cleanup;
    }).catch((error) => {
      if (!disposed) {
        toast.error("无法接收任务进度", {
          description: toUserErrorMessage(error, "任务仍可继续执行，请稍后查看导出结果。"),
        });
      }
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    const storage = browserStorage();
    if (running || !storage) return;
    saveRunSession(storage, {
      startDate,
      endDate,
      startTime,
      endTime,
      selectedGroups,
      options,
      logs,
      result,
    });
  }, [endDate, endTime, logs, options, result, running, selectedGroups, startDate, startTime]);

  const runs = useMemo(() => normalizeRuns(result, selectedGroups), [result, selectedGroups]);
  const pendingSyncRuns = useMemo(
    () => runs.filter((run) => (run.smartSheetPreview?.pending ?? 0) > 0),
    [runs],
  );
  const pendingSyncCount = pendingSyncRuns.reduce(
    (total, run) => total + (run.smartSheetPreview?.pending ?? 0),
    0,
  );

  const loadGroups = async () => {
    setLoadingGroups(true);
    try {
      const response = await bridge.listGroups();
      setGroups(response.groups);
      toast.success(`已读取 ${response.groups.length} 个群聊，可多选`);
    } catch (error) {
      toast.error("无法读取群聊", {
        description: toUserErrorMessage(error, "请确认企业微信数据目录和密钥配置正确。"),
      });
    } finally {
      setLoadingGroups(false);
    }
  };

  const execute = async () => {
    if (!selectedGroups.length) {
      toast.warning("请至少选择一个企业微信群");
      return;
    }
    const rangeError = validateRunRange(startDate, startTime, endDate, endTime);
    if (rangeError) {
      toast.warning(rangeError);
      return;
    }
    if (!options.exportXlsx && !options.exportMarkdown && !options.runAnalysis) {
      toast.warning("请至少选择一种导出或分析方式");
      return;
    }
    if (options.prepareSmartSheet && !options.runAnalysis) {
      toast.warning("同步 Smart Sheet 前需要启用大模型分析");
      return;
    }

    const request: TaskRequest = {
      startDate,
      endDate,
      date: endDate,
      startTime,
      endTime,
      groups: selectedGroups,
      ...options,
    };
    setRunning(true);
    setConfirmingSync(false);
    setLogs([
      `任务已创建：${startDate} ${startTime} → ${endDate} ${endTime}`,
      `将顺序处理 ${selectedGroups.length} 个群聊`,
    ]);
    try {
      const nextResult = await bridge.runTask(request);
      setResult(nextResult);
      setLogs((previous) => [...previous, `全部完成，共 ${selectedGroups.length} 个群聊`]);
      const nextRuns = normalizeRuns(nextResult, selectedGroups);
      if (nextRuns.some((run) => (run.smartSheetPreview?.pending ?? 0) > 0)) {
        setConfirmingSync(true);
      }
      toast.success(`${selectedGroups.length} 个群聊处理完成`);
    } catch (error) {
      const message = toUserErrorMessage(error, "请检查配置后重试。");
      setLogs((previous) => [...previous, `处理未完成：${message}`]);
      toast.error("处理失败", { description: message });
    } finally {
      setRunning(false);
    }
  };

  const confirmSync = async () => {
    if (!pendingSyncRuns.length) return;
    setSyncing(true);
    try {
      let synced = 0;
      for (const run of pendingSyncRuns) {
        const response = await bridge.syncSmartSheet(run.dayDir, run.smartSheetDate ?? endDate);
        synced += response.synced ?? run.smartSheetPreview?.pending ?? 0;
      }
      toast.success(`已向腾讯文档写入 ${synced} 条问题`);
      setConfirmingSync(false);
    } catch (error) {
      toast.error("腾讯文档同步失败", {
        description: toUserErrorMessage(error, "本地文件不受影响，请稍后重试。"),
      });
    } finally {
      setSyncing(false);
    }
  };

  const setWholeDay = () => {
    setStartTime("00:00");
    setEndTime("23:59");
  };

  const setSingleDay = (date: string) => {
    setStartDate(date);
    setEndDate(date);
  };

  const openResultPath = async (path: string) => {
    try {
      await bridge.openPath(path);
    } catch (error) {
      toast.error("无法打开导出结果", {
        description: toUserErrorMessage(error, "文件可能已被移动或删除，请重新导出。"),
      });
    }
  };

  return (
    <div className="page-content">
      <div className="page-title-row">
        <div>
          <div className="eyebrow"><Sparkles size={13} />Export workspace</div>
          <h1>新建导出任务</h1>
          <p>一次选择多个群聊和精确时间区间，统一导出或分析。</p>
        </div>
        <div className="ready-badge"><span className="status-dot" />数据仅在本机处理</div>
      </div>

      <div className="run-layout">
        <div className="run-primary">
          <section className="glass-card task-step-card">
            <div className="step-index">1</div>
            <SectionHeader title="选择导出时间" description="支持跨天范围，结束日期最晚为今天。" />
            <div className="form-grid two-columns">
              <Field label="开始日期">
                <div className="input-with-icon">
                  <CalendarDays size={16} />
                  <input
                    type="date"
                    value={startDate}
                    max={endDate}
                    disabled={running}
                    onChange={(event) => setStartDate(event.target.value)}
                  />
                </div>
              </Field>
              <Field label="开始时间">
                <div className="input-with-icon">
                  <Clock3 size={16} />
                  <input type="time" value={startTime} disabled={running} onChange={(event) => setStartTime(event.target.value)} />
                </div>
              </Field>
              <Field label="结束日期">
                <div className="input-with-icon">
                  <CalendarDays size={16} />
                  <input
                    type="date"
                    value={endDate}
                    min={startDate}
                    max={today}
                    disabled={running}
                    onChange={(event) => setEndDate(event.target.value)}
                  />
                </div>
              </Field>
              <Field label="结束时间">
                <div className="input-with-icon">
                  <Clock3 size={16} />
                  <input type="time" value={endTime} disabled={running} onChange={(event) => setEndTime(event.target.value)} />
                </div>
              </Field>
            </div>
            <div className="quick-range-row">
              <span>快捷选择</span>
              <button type="button" disabled={running} onClick={setWholeDay}>全天</button>
              <button type="button" disabled={running} onClick={() => { setStartTime("09:00"); setEndTime("18:00"); }}>工作时间 09:00–18:00</button>
              <button type="button" disabled={running} onClick={() => setSingleDay(localDate(-1))}>昨天</button>
              <button type="button" disabled={running} onClick={() => setSingleDay(today)}>今天</button>
              <button type="button" disabled={running} onClick={() => { setStartDate(localDate(-1)); setEndDate(today); }}>昨天至今天</button>
            </div>
          </section>

          <section className="glass-card task-step-card">
            <div className="step-index">2</div>
            <SectionHeader
              title="选择群聊"
              description="可以搜索并多选；每个群的结果会保存到独立目录。"
              action={<span className="selection-count"><UsersRound size={14} />已选 {selectedGroups.length} 个</span>}
            />
            <GroupMultiSelect
              groups={groups}
              selected={selectedGroups}
              loading={loadingGroups}
              onReload={() => void loadGroups()}
              onChange={setSelectedGroups}
            />
          </section>

          <section className="glass-card task-step-card">
            <div className="step-index">3</div>
            <SectionHeader title="选择处理方式" description="原始聊天导出和 AI 分析可以独立启用。" />
            <ProcessingOptionsEditor config={config} value={options} onChange={setOptions} />
          </section>

          <div className="execute-bar">
            <div>
              <strong>{selectedGroups.length || 0} 个群聊 · {startDate} {startTime}</strong>
              <span>至 {endDate} {endTime} · {options.exportXlsx ? "Excel" : ""}{options.exportXlsx && options.exportMarkdown ? " + " : ""}{options.exportMarkdown ? "Markdown" : ""}</span>
            </div>
            <Button className="run-button" disabled={running} onClick={() => void execute()}>
              {running ? <LoaderCircle className="spin" size={18} /> : <Play size={18} fill="currentColor" />}
              {running ? `正在处理 ${selectedGroups.length} 个群…` : "开始执行"}
            </Button>
          </div>
        </div>

        <aside className="run-secondary">
          <section className="glass-card status-card">
            <SectionHeader title="运行状态" description={running ? "任务正在后台顺序执行" : runs.length ? "最近一次任务已完成" : "配置完成后即可执行"} />
            <div className="progress-rail"><span className={running ? "progress-live" : runs.length ? "progress-done" : ""} /></div>
            <div className="log-list">
              {logs.length ? logs.slice(-9).map((log, index) => (
                <div className="log-line" key={`${index}-${log}`}>
                  {index === logs.slice(-9).length - 1 && running ? <LoaderCircle className="spin" size={14} /> : <CheckCircle2 size={14} />}
                  <span>{log}</span>
                </div>
              )) : <div className="empty-log">这里会显示每个群的处理阶段</div>}
            </div>
          </section>

          <section className="glass-card output-card">
            <SectionHeader title="导出结果" description={runs.length ? `已生成 ${runs.length} 组结果` : undefined} />
            {runs.length ? (
              <div className="multi-output-list">
                {runs.map((run) => (
                  <div className="group-output" key={run.groupId}>
                    <div className="group-output-heading"><span>{run.groupName}</span><small>{Object.keys(run.outputs).length} 个文件</small></div>
                    <div className="output-list">
                      {Object.entries(run.outputs).map(([kind, path]) => (
                        <button key={kind} onClick={() => void openResultPath(path)}>
                          <span className={`file-icon file-${kind}`}>{kind === "xlsx" ? <FileSpreadsheet size={17} /> : <FileText size={17} />}</span>
                          <span><strong>{kind === "xlsx" ? "Excel 工作簿" : "Markdown 归档"}</strong><small>{path}</small></span>
                          <ExternalLink size={13} />
                        </button>
                      ))}
                    </div>
                    <button className="open-folder-link" onClick={() => void openResultPath(run.dayDir)}><FolderOpen size={13} />打开完整目录</button>
                  </div>
                ))}
              </div>
            ) : <div className="output-placeholder"><FileText size={28} /><span>执行后可按群打开导出文件</span></div>}
          </section>
        </aside>
      </div>

      {confirmingSync && pendingSyncCount > 0 && (
        <div className="modal-backdrop">
          <div className="modal-card">
            <div className="modal-icon"><CloudUpload size={24} /></div>
            <h2>确认写入腾讯文档？</h2>
            <p><strong>{pendingSyncRuns.length}</strong> 个群共有 <strong>{pendingSyncCount}</strong> 条新问题待写入。已同步记录会自动跳过。</p>
            <div className="modal-note">这是外部写入操作。取消不会影响已经生成的本地文件。</div>
            <div className="modal-actions">
              <Button variant="secondary" onClick={() => setConfirmingSync(false)}>暂不同步</Button>
              <Button disabled={syncing} onClick={() => void confirmSync()}>{syncing && <LoaderCircle className="spin" size={16} />}确认写入 {pendingSyncCount} 条</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

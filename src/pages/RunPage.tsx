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
  Table2,
  UsersRound,
} from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import {
  shouldOpenSmartSheetPreview,
  smartSheetConfigurationBlockers,
} from "../lib/smartSheetSync";
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
  resolveSmartSheetTemplateId,
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
    issueCount: result.issueCount,
    smartSheetPreview: result.smartSheetPreview,
  }];
}

export function analysisResultLabel(
  result: Pick<TaskRunResult, "issueCount">,
): string | null {
  if (result.issueCount === undefined) return null;
  if (result.issueCount === 0) return "模型未识别到问题";
  return `识别 ${result.issueCount} 个问题`;
}

const frozenTemplateId = (run: TaskRunResult) =>
  run.smartSheetTemplateId || run.smartSheetPreview?.template_id || "";

const frozenSyncDate = (run: TaskRunResult) =>
  run.smartSheetDate || run.endDate || run.startDate || "";

const frozenDefinitionPath = (run: TaskRunResult) => run.definitionPath?.trim() || "";

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
  const [refreshingSyncPreview, setRefreshingSyncPreview] = useState(false);
  const [syncPreviewError, setSyncPreviewError] = useState("");

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
  const smartSheetPreviewRuns = useMemo(
    () => runs.filter((run) => run.smartSheetPreview != null),
    [runs],
  );
  const pendingSyncRuns = useMemo(
    () => smartSheetPreviewRuns.filter((run) => (run.smartSheetPreview?.pending ?? 0) > 0),
    [smartSheetPreviewRuns],
  );
  const pendingSyncCount = pendingSyncRuns.reduce(
    (total, run) => total + (run.smartSheetPreview?.pending ?? 0),
    0,
  );
  const smartSheetTotalCount = smartSheetPreviewRuns.reduce(
    (total, run) => total + (run.smartSheetPreview?.total
      ?? ((run.smartSheetPreview?.pending ?? 0) + (run.smartSheetPreview?.already_synced ?? 0))),
    0,
  );
  const alreadySyncedCount = smartSheetPreviewRuns.reduce(
    (total, run) => total + (run.smartSheetPreview?.already_synced ?? 0),
    0,
  );
  const selectedSmartSheetTemplateId = resolveSmartSheetTemplateId(config, options.promptId, options.smartSheetTemplateId);
  const smartSheetPreviewTemplates = useMemo(() => {
    const templates = new Map<string, { id: string; name: string; url: string; groups: string[] }>();
    smartSheetPreviewRuns.forEach((run) => {
      const id = frozenTemplateId(run);
      const configured = config.smart_sheet.templates.find((template) => template.id === id);
      const current = templates.get(id) ?? {
        id,
        name: run.smartSheetTemplateName || run.smartSheetPreview?.template_name || configured?.name || id || "默认腾讯文档模板",
        url: run.smartSheetTemplateUrl || run.smartSheetPreview?.template_url || configured?.url || "",
        groups: [],
      };
      current.groups.push(run.groupName);
      templates.set(id, current);
    });
    return [...templates.values()];
  }, [config.smart_sheet.templates, smartSheetPreviewRuns]);
  const syncWarnings = useMemo(() => {
    return smartSheetConfigurationBlockers(pendingSyncRuns);
  }, [pendingSyncRuns]);
  const syncBlockers = useMemo(() => {
    const messages = new Set<string>();
    pendingSyncRuns.forEach((run) => {
      if (!frozenTemplateId(run)) messages.add(`${run.groupName} 缺少冻结的腾讯文档模板 ID`);
      if (!frozenSyncDate(run)) messages.add(`${run.groupName} 缺少同步日期`);
      if (!run.dayDir) messages.add(`${run.groupName} 缺少本地结果目录`);
      if (!frozenDefinitionPath(run)) messages.add(`${run.groupName} 缺少不可变的问题定义快照`);
      if (!run.smartSheetPreview?.template_revision) {
        messages.add(`${run.groupName} 的当前腾讯文档模板缺少 revision，无法安全确认写入`);
      }
      if (!run.smartSheetPreview?.document_revision) {
        messages.add(`${run.groupName} 的问题定义快照缺少 document revision，无法安全确认写入`);
      }
    });
    return [...messages];
  }, [pendingSyncRuns]);

  const openSyncConfirmation = async (sourceRuns = pendingSyncRuns) => {
    const candidates = sourceRuns.filter((run) => (run.smartSheetPreview?.pending ?? 0) > 0);
    if (!candidates.length) {
      if (shouldOpenSmartSheetPreview(sourceRuns)) setConfirmingSync(true);
      return;
    }
    setConfirmingSync(true);
    setRefreshingSyncPreview(true);
    setSyncPreviewError("");
    try {
      const missingFrozenData = candidates.flatMap((run) => [
        !frozenTemplateId(run) ? `${run.groupName} 缺少冻结的腾讯文档模板 ID` : "",
        !frozenSyncDate(run) ? `${run.groupName} 缺少同步日期` : "",
        !run.dayDir ? `${run.groupName} 缺少本地结果目录` : "",
        !frozenDefinitionPath(run) ? `${run.groupName} 缺少不可变的问题定义快照` : "",
      ]).filter(Boolean);
      if (missingFrozenData.length) throw new Error(missingFrozenData.join("；"));

      const refreshedRuns = await Promise.all(candidates.map(async (run) => {
        const preview = await bridge.previewSmartSheet(
          run.dayDir,
          frozenSyncDate(run),
          frozenTemplateId(run),
          frozenDefinitionPath(run),
        );
        if (!preview.template_revision) {
          throw new Error(`${run.groupName} 的当前腾讯文档模板缺少 revision`);
        }
        if (!preview.document_revision) {
          throw new Error(`${run.groupName} 的问题定义快照缺少 document revision`);
        }
        return {
          ...run,
          smartSheetTemplateName: preview.template_name ?? run.smartSheetTemplateName,
          smartSheetTemplateUrl: preview.template_url ?? run.smartSheetTemplateUrl,
          smartSheetPreview: preview,
        };
      }));
      const refreshedByRun = new Map(
        refreshedRuns.map((run) => [`${run.groupId}\u0000${run.dayDir}`, run]),
      );
      setResult((current) => {
        if (!current) return current;
        const nextRuns = current.runs.map((run) => (
          refreshedByRun.get(`${run.groupId}\u0000${run.dayDir}`) ?? run
        ));
        const topLevelRun = refreshedRuns.find((run) => run.dayDir === current.dayDir);
        return {
          ...current,
          runs: nextRuns,
          smartSheetPreview: topLevelRun?.smartSheetPreview ?? current.smartSheetPreview,
        };
      });
      const refreshedPending = refreshedRuns.reduce(
        (total, run) => total + (run.smartSheetPreview?.pending ?? 0),
        0,
      );
      if (refreshedPending === 0) {
        toast.info("当前结果已没有待同步记录");
      }
    } catch (error) {
      const message = toUserErrorMessage(error, "无法按当前配置刷新腾讯文档预览，请修复后重试。");
      setSyncPreviewError(message);
      toast.error("待确认预览刷新失败", { description: message });
    } finally {
      setRefreshingSyncPreview(false);
    }
  };

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
    if (pendingSyncCount > 0) {
      toast.warning("请先处理上一次腾讯文档待同步结果", {
        description: "完成同步，或在确认框中明确选择“放弃待同步”后，才能开始新任务。",
      });
      return;
    }
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
    if (options.prepareSmartSheet && !selectedSmartSheetTemplateId) {
      toast.warning("请先配置并选择腾讯文档模板");
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
      smartSheetTemplateId: selectedSmartSheetTemplateId,
    };
    if (options.smartSheetTemplateId !== selectedSmartSheetTemplateId) {
      setOptions((current) => ({ ...current, smartSheetTemplateId: selectedSmartSheetTemplateId }));
    }
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
      if (shouldOpenSmartSheetPreview(nextRuns)) {
        if (nextRuns.some((run) => (run.smartSheetPreview?.pending ?? 0) > 0)) {
          void openSyncConfirmation(nextRuns);
        } else {
          setConfirmingSync(true);
        }
      }
      const emptyAnalysisRuns = nextRuns.filter((run) => run.issueCount === 0);
      if (emptyAnalysisRuns.length) {
        const names = emptyAnalysisRuns.map((run) => run.groupName).join("、");
        toast.warning("分析完成，但模型未识别到问题", {
          description: `${names} 的聊天记录已正常导出，问题清单为空。`,
        });
      } else {
        toast.success(`${selectedGroups.length} 个群聊处理完成`);
      }
    } catch (error) {
      const message = toUserErrorMessage(error, "请检查配置后重试。");
      setLogs((previous) => [...previous, `处理未完成：${message}`]);
      toast.error("处理失败", { description: message });
    } finally {
      setRunning(false);
    }
  };

  const confirmSync = async () => {
    if (
      !pendingSyncRuns.length
      || refreshingSyncPreview
      || syncPreviewError
      || syncWarnings.length
      || syncBlockers.length
    ) return;
    setSyncing(true);
    try {
      let synced = 0;
      for (const run of pendingSyncRuns) {
        const response = await bridge.syncSmartSheet(
          run.dayDir,
          frozenSyncDate(run),
          frozenTemplateId(run),
          true,
          run.smartSheetPreview?.template_revision || "",
          frozenDefinitionPath(run),
          run.smartSheetPreview?.document_revision || "",
        );
        synced += response.synced ?? run.smartSheetPreview?.pending ?? 0;
        setResult((current) => {
          if (!current) return current;
          const updatePreview = (preview: TaskRunResult["smartSheetPreview"]) => preview
            ? {
              ...preview,
              pending: 0,
              already_synced: response.total
                ?? ((preview.already_synced ?? 0) + (response.synced ?? 0)),
            }
            : preview;
          const nextRuns = current.runs.map((candidate) => (
            candidate.groupId === run.groupId && candidate.dayDir === run.dayDir
              ? { ...candidate, smartSheetPreview: updatePreview(candidate.smartSheetPreview) }
              : candidate
          ));
          const topLevelPreview = current.dayDir === run.dayDir
            ? updatePreview(current.smartSheetPreview)
            : current.smartSheetPreview;
          return { ...current, runs: nextRuns, smartSheetPreview: topLevelPreview };
        });
      }
      toast.success(`已向腾讯文档写入 ${synced} 条问题`);
      setConfirmingSync(false);
    } catch (error) {
      toast.error("腾讯文档同步失败", {
        description: toUserErrorMessage(error, "本地文件不受影响。中断后请先核对目标文档，再决定是否重试。"),
      });
    } finally {
      setSyncing(false);
    }
  };

  const discardPendingSync = () => {
    if (!window.confirm("确定放弃本次腾讯文档待同步结果吗？本地导出文件不会被删除。")) return;
    setResult((current) => {
      if (!current) return current;
      const clearPreview = (preview: TaskRunResult["smartSheetPreview"]) => preview
        ? { ...preview, pending: 0 }
        : preview;
      return {
        ...current,
        runs: current.runs.map((run) => ({
          ...run,
          smartSheetPreview: clearPreview(run.smartSheetPreview),
        })),
        smartSheetPreview: clearPreview(current.smartSheetPreview),
      };
    });
    setConfirmingSync(false);
    setSyncPreviewError("");
    toast.success("已放弃腾讯文档待同步结果，本地文件仍然保留");
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
            <Button
              className="run-button"
              disabled={running || pendingSyncCount > 0}
              title={pendingSyncCount > 0 ? "请先同步或放弃上一次待同步结果" : undefined}
              onClick={() => void execute()}
            >
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
                {runs.map((run) => {
                  const resultLabel = analysisResultLabel(run);
                  return (
                    <div className="group-output" key={run.groupId}>
                      <div className="group-output-heading">
                        <span>{run.groupName}</span>
                        <small className={run.issueCount === 0 ? "analysis-result-empty" : undefined}>
                          {Object.keys(run.outputs).length} 个文件
                          {resultLabel ? ` · ${resultLabel}` : ""}
                        </small>
                      </div>
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
                  );
                })}
              </div>
            ) : <div className="output-placeholder"><FileText size={28} /><span>执行后可按群打开导出文件</span></div>}
            {!confirmingSync && pendingSyncCount > 0 && (
              <div className="pending-sync-action">
                <span>仍有 {pendingSyncCount} 条问题尚未写入腾讯文档</span>
                <Button variant="secondary" disabled={refreshingSyncPreview} onClick={() => void openSyncConfirmation()}>
                  <CloudUpload size={14} />继续同步
                </Button>
              </div>
            )}
          </section>
        </aside>
      </div>

      {confirmingSync && smartSheetPreviewRuns.length > 0 && (
        <div className="modal-backdrop">
          <div className="modal-card smart-sheet-sync-modal">
            <div className="modal-icon"><CloudUpload size={24} /></div>
            <h2>{pendingSyncCount > 0 ? "确认写入腾讯文档？" : "腾讯文档写入预览"}</h2>
            {refreshingSyncPreview
              ? <p><LoaderCircle className="spin" size={15} /> 正在按当前配置刷新腾讯文档预览…</p>
              : syncPreviewError
                ? <p>当前预览刷新失败，本次确认已被阻止。</p>
                : pendingSyncCount > 0
                  ? <p><strong>{pendingSyncRuns.length}</strong> 个群共有 <strong>{pendingSyncCount}</strong> 条新问题待写入。仅已取得远端记录 ID 并成功写入本地台账的记录会跳过。</p>
                  : smartSheetTotalCount > 0
                    ? <p>本次识别的 <strong>{smartSheetTotalCount}</strong> 条问题均已写入过腾讯文档，其中 <strong>{alreadySyncedCount}</strong> 条已记录在本地同步台账中，无需重复写入。</p>
                    : <p>本次没有识别到可写入腾讯文档的新问题。</p>}
            <div className="sync-template-list">
              {smartSheetPreviewTemplates.map((template) => (
                <div key={template.id}>
                  <span><Table2 size={14} /></span>
                  <span><strong>{template.name}</strong><small>{template.groups.join("、")}{template.url ? ` · ${template.url}` : ""}</small></span>
                </div>
              ))}
            </div>
            {syncWarnings.length > 0 && <div className="sync-blockers schedule-sync-warnings">
              {syncWarnings.map((message) => <span key={message}>{message}</span>)}
              <small>以上是按当前模板配置重新预检的结果；真正写入前仍会再次校验。</small>
            </div>}
            {syncPreviewError && <div className="sync-blockers"><span>{syncPreviewError}</span></div>}
            {syncBlockers.length > 0 && <div className="sync-blockers">{syncBlockers.map((message) => <span key={message}>{message}</span>)}</div>}
            <div className="modal-note">{refreshingSyncPreview
              ? "刷新预览只读取当前本地结果和模板配置，不会向腾讯文档写入数据。"
              : syncPreviewError
                ? "修复配置后点击“刷新预览”，待确认结果和本地文件都会保留。"
                : syncWarnings.length
                  ? "当前字段映射或 Webhook 配置无效，请先到设置中修复并保存，再刷新预览。"
                : syncBlockers.length
                  ? "历史结果缺少安全同步所需的冻结信息，请重新执行任务。"
                  : pendingSyncCount > 0
                    ? "这是外部写入操作。取消不会影响已经生成的本地文件。"
                    : "预览已完成，没有向腾讯文档重复写入数据。"}</div>
            <div className="modal-actions">
              {pendingSyncCount > 0 ? <>
                <Button variant="danger" disabled={syncing || refreshingSyncPreview} onClick={discardPendingSync}>放弃待同步</Button>
                <Button variant="secondary" disabled={syncing || refreshingSyncPreview} onClick={() => void openSyncConfirmation()}>刷新预览</Button>
                <Button variant="secondary" disabled={syncing} onClick={() => setConfirmingSync(false)}>暂不同步</Button>
                <Button
                  disabled={
                    syncing
                    || refreshingSyncPreview
                    || Boolean(syncPreviewError)
                    || syncWarnings.length > 0
                    || syncBlockers.length > 0
                  }
                  onClick={() => void confirmSync()}
                >{syncing && <LoaderCircle className="spin" size={16} />}确认写入 {pendingSyncCount} 条</Button>
              </> : (
                <Button onClick={() => setConfirmingSync(false)}>关闭</Button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

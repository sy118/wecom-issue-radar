import { useEffect, useMemo, useState } from "react";
import {
  CalendarClock,
  Clock3,
  Edit3,
  Info,
  LoaderCircle,
  Play,
  Plus,
  Power,
  Trash2,
  UsersRound,
} from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import type {
  AppConfig,
  GroupInfo,
  ProcessingOptions,
  ScheduleDefinition,
  ScheduleEvent,
  ScheduleDateMode,
  TaskGroup,
} from "../types";
import { Button, Field, Input, SectionHeader, Switch } from "../components/ui";
import {
  defaultProcessingOptions,
  GroupMultiSelect,
  ProcessingOptionsEditor,
} from "../components/TaskEditor";

const weekdays = [
  { value: 1, label: "一" },
  { value: 2, label: "二" },
  { value: 3, label: "三" },
  { value: 4, label: "四" },
  { value: 5, label: "五" },
  { value: 6, label: "六" },
  { value: 7, label: "日" },
];

const localDate = () => {
  const date = new Date();
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
};

const newSchedule = (config: AppConfig): ScheduleDefinition => ({
  id: `schedule_${Date.now()}`,
  name: "每日群聊导出",
  enabled: true,
  runAt: "18:30",
  weekdays: [1, 2, 3, 4, 5],
  dateMode: "today",
  fixedDate: localDate(),
  startTime: "00:00",
  endTime: "23:59",
  groups: [],
  ...defaultProcessingOptions(config),
});

const dateModeLabel: Record<ScheduleDateMode, string> = {
  today: "执行当天",
  yesterday: "执行前一天",
  fixed: "固定日期",
};

const processingFrom = (schedule: ScheduleDefinition): ProcessingOptions => ({
  promptId: schedule.promptId,
  runOcr: schedule.runOcr,
  runAnalysis: schedule.runAnalysis,
  exportXlsx: schedule.exportXlsx,
  exportMarkdown: schedule.exportMarkdown,
  prepareSmartSheet: schedule.prepareSmartSheet,
});

export function SchedulesPage({ config }: { config: AppConfig }) {
  const [schedules, setSchedules] = useState<ScheduleDefinition[]>(config.schedules ?? []);
  const [groups, setGroups] = useState<GroupInfo[]>([]);
  const [loadingGroups, setLoadingGroups] = useState(false);
  const [saving, setSaving] = useState(false);
  const [runningId, setRunningId] = useState("");
  const [editor, setEditor] = useState<ScheduleDefinition | null>(null);
  const [activity, setActivity] = useState<Record<string, string>>({});

  useEffect(() => {
    void bridge.listSchedules().then(setSchedules).catch((error) => {
      toast.error(`读取定时任务失败：${String(error)}`);
    });
    let disposed = false;
    const cleanups: Array<() => void> = [];
    void bridge.onScheduleProgress((event) => {
      if (!disposed) setActivity((current) => ({ ...current, [event.scheduleId]: event.message }));
    }).then((cleanup) => cleanups.push(cleanup));
    void bridge.onScheduleCompleted((event) => {
      if (disposed) return;
      setActivity((current) => ({ ...current, [event.scheduleId]: event.message }));
      setRunningId("");
      if (event.success === false) toast.error(`${event.scheduleName}：${event.message}`);
      else toast.success(`${event.scheduleName} 执行完成`);
    }).then((cleanup) => cleanups.push(cleanup));
    return () => {
      disposed = true;
      cleanups.forEach((cleanup) => cleanup());
    };
  }, []);

  const enabledCount = schedules.filter((schedule) => schedule.enabled).length;
  const sortedSchedules = useMemo(
    () => [...schedules].sort((left, right) => left.runAt.localeCompare(right.runAt)),
    [schedules],
  );

  const loadGroups = async () => {
    setLoadingGroups(true);
    try {
      const response = await bridge.listGroups();
      setGroups(response.groups);
      toast.success(`已读取 ${response.groups.length} 个群聊`);
    } catch (error) {
      toast.error(`读取群聊失败：${String(error)}`);
    } finally {
      setLoadingGroups(false);
    }
  };

  const persist = async (next: ScheduleDefinition[], message?: string) => {
    setSaving(true);
    try {
      const saved = await bridge.saveSchedules(next);
      setSchedules(saved);
      if (message) toast.success(message);
    } catch (error) {
      toast.error(`保存定时任务失败：${String(error)}`);
      throw error;
    } finally {
      setSaving(false);
    }
  };

  const saveEditor = async () => {
    if (!editor) return;
    if (!editor.name.trim()) return toast.warning("请输入任务名称");
    if (!editor.groups.length) return toast.warning("请至少选择一个群聊");
    if (!editor.weekdays.length) return toast.warning("请至少选择一个执行日");
    if (editor.startTime > editor.endTime) return toast.warning("开始时间不能晚于结束时间");
    if (editor.prepareSmartSheet && !editor.runAnalysis) return toast.warning("准备 Smart Sheet 前需要启用大模型分析");
    const exists = schedules.some((schedule) => schedule.id === editor.id);
    const next = exists
      ? schedules.map((schedule) => schedule.id === editor.id ? editor : schedule)
      : [...schedules, editor];
    try {
      await persist(next, exists ? "定时任务已更新" : "定时任务已创建");
      setEditor(null);
    } catch {
      // Error toast is handled by persist.
    }
  };

  const toggleEnabled = async (schedule: ScheduleDefinition) => {
    const next = schedules.map((item) => item.id === schedule.id ? { ...item, enabled: !item.enabled } : item);
    try {
      await persist(next);
    } catch {
      // Error toast is handled by persist.
    }
  };

  const remove = async (schedule: ScheduleDefinition) => {
    if (!window.confirm(`确定删除“${schedule.name}”吗？`)) return;
    try {
      await persist(schedules.filter((item) => item.id !== schedule.id), "定时任务已删除");
    } catch {
      // Error toast is handled by persist.
    }
  };

  const runNow = async (schedule: ScheduleDefinition) => {
    setRunningId(schedule.id);
    setActivity((current) => ({ ...current, [schedule.id]: "正在准备立即执行…" }));
    try {
      await bridge.runScheduleNow(schedule.id);
    } catch (error) {
      setRunningId("");
      toast.error(`启动任务失败：${String(error)}`);
    }
  };

  const updateEditor = (patch: Partial<ScheduleDefinition>) => {
    setEditor((current) => current ? { ...current, ...patch } : current);
  };

  const updateProcessing = (processing: ProcessingOptions) => updateEditor(processing);
  const updateWeekday = (day: number) => {
    if (!editor) return;
    updateEditor({
      weekdays: editor.weekdays.includes(day)
        ? editor.weekdays.filter((value) => value !== day)
        : [...editor.weekdays, day].sort(),
    });
  };

  return (
    <div className="page-content schedules-page">
      <div className="page-title-row">
        <div>
          <div className="eyebrow"><CalendarClock size={13} />Automation</div>
          <h1>定时导出</h1>
          <p>按设定时间自动处理多个群聊，动态选择当天或前一天记录。</p>
        </div>
        <Button onClick={() => setEditor(newSchedule(config))}><Plus size={16} />新建定时任务</Button>
      </div>

      <div className="schedule-overview">
        <div className="glass-card schedule-stat"><span><Power size={17} /></span><div><strong>{enabledCount}</strong><small>正在运行的任务</small></div></div>
        <div className="glass-card schedule-stat"><span><CalendarClock size={17} /></span><div><strong>{schedules.length}</strong><small>全部定时任务</small></div></div>
        <div className="scheduler-note"><Info size={16} /><span>当前版本需保持应用运行；到点后任务在后台自动执行，每个群结果独立保存。</span></div>
      </div>

      <section className="glass-card schedule-list-card">
        <SectionHeader title="任务列表" description="开关关闭后会保留配置，但不会自动执行。" />
        {sortedSchedules.length ? (
          <div className="schedule-list">
            {sortedSchedules.map((schedule) => (
              <article className={schedule.enabled ? "schedule-row" : "schedule-row disabled"} key={schedule.id}>
                <button
                  type="button"
                  className={schedule.enabled ? "schedule-power enabled" : "schedule-power"}
                  title={schedule.enabled ? "暂停任务" : "启用任务"}
                  disabled={saving}
                  onClick={() => void toggleEnabled(schedule)}
                >
                  <Power size={15} />
                </button>
                <div className="schedule-time"><strong>{schedule.runAt}</strong><small>{schedule.enabled ? "已启用" : "已暂停"}</small></div>
                <div className="schedule-main">
                  <strong>{schedule.name}</strong>
                  <div className="schedule-meta">
                    <span><UsersRound size={12} />{schedule.groups.length} 个群</span>
                    <span><Clock3 size={12} />{schedule.startTime}–{schedule.endTime}</span>
                    <span>{dateModeLabel[schedule.dateMode]}</span>
                    <span>周{schedule.weekdays.map((day) => weekdays.find((item) => item.value === day)?.label).join("、")}</span>
                  </div>
                  {activity[schedule.id] && <p className="schedule-activity">{activity[schedule.id]}</p>}
                </div>
                <div className="schedule-actions">
                  <Button variant="secondary" disabled={runningId === schedule.id} onClick={() => void runNow(schedule)}>
                    {runningId === schedule.id ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}立即执行
                  </Button>
                  <Button variant="ghost" title="编辑" onClick={() => setEditor({ ...schedule, groups: [...schedule.groups], weekdays: [...schedule.weekdays] })}><Edit3 size={15} /></Button>
                  <Button variant="ghost" title="删除" onClick={() => void remove(schedule)}><Trash2 size={15} /></Button>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <div className="schedule-empty">
            <div><CalendarClock size={27} /></div>
            <strong>还没有定时任务</strong>
            <p>创建后，应用会在指定日期和时间自动导出群聊。</p>
            <Button variant="secondary" onClick={() => setEditor(newSchedule(config))}><Plus size={15} />创建第一个任务</Button>
          </div>
        )}
      </section>

      {editor && (
        <div className="modal-backdrop schedule-modal-backdrop">
          <div className="schedule-modal">
            <div className="schedule-modal-header">
              <div><span className="eyebrow">Schedule editor</span><h2>{schedules.some((item) => item.id === editor.id) ? "编辑定时任务" : "新建定时任务"}</h2></div>
              <button type="button" onClick={() => setEditor(null)}>×</button>
            </div>
            <div className="schedule-modal-body">
              <section className="schedule-editor-section">
                <h3>基本信息</h3>
                <div className="form-grid two-columns">
                  <Field label="任务名称"><Input value={editor.name} onChange={(event) => updateEditor({ name: event.target.value })} /></Field>
                  <Field label="每天执行时间"><Input type="time" value={editor.runAt} onChange={(event) => updateEditor({ runAt: event.target.value })} /></Field>
                </div>
                <Field label="每周执行日">
                  <div className="weekday-picker">
                    {weekdays.map((day) => <button type="button" key={day.value} className={editor.weekdays.includes(day.value) ? "selected" : ""} onClick={() => updateWeekday(day.value)}>周{day.label}</button>)}
                  </div>
                </Field>
              </section>

              <section className="schedule-editor-section">
                <h3>导出时间范围</h3>
                <div className="date-mode-picker">
                  {(["today", "yesterday", "fixed"] as ScheduleDateMode[]).map((mode) => (
                    <button type="button" key={mode} className={editor.dateMode === mode ? "selected" : ""} onClick={() => updateEditor({ dateMode: mode })}>
                      <strong>{dateModeLabel[mode]}</strong>
                      <small>{mode === "today" ? "每天动态取当天" : mode === "yesterday" ? "每天动态取前一天" : "始终导出指定日期"}</small>
                    </button>
                  ))}
                </div>
                <div className="form-grid schedule-range-grid">
                  {editor.dateMode === "fixed" && <Field label="固定日期"><Input type="date" value={editor.fixedDate} onChange={(event) => updateEditor({ fixedDate: event.target.value })} /></Field>}
                  <Field label="开始时间"><Input type="time" value={editor.startTime} onChange={(event) => updateEditor({ startTime: event.target.value })} /></Field>
                  <Field label="结束时间"><Input type="time" value={editor.endTime} onChange={(event) => updateEditor({ endTime: event.target.value })} /></Field>
                </div>
              </section>

              <section className="schedule-editor-section">
                <div className="schedule-section-heading"><h3>执行群聊</h3><span>已选 {editor.groups.length} 个</span></div>
                <GroupMultiSelect groups={groups} selected={editor.groups} loading={loadingGroups} onReload={() => void loadGroups()} onChange={(next: TaskGroup[]) => updateEditor({ groups: next })} />
              </section>

              <section className="schedule-editor-section">
                <h3>处理方式</h3>
                <ProcessingOptionsEditor
                  config={config}
                  value={processingFrom(editor)}
                  onChange={updateProcessing}
                  smartSheetHint="定时任务仅生成待同步预览，不自动写云端"
                />
              </section>

              <Switch checked={editor.enabled} onChange={(enabled) => updateEditor({ enabled })} label="保存后立即启用" description="关闭时仅保存任务配置" />
            </div>
            <div className="schedule-modal-footer">
              <Button variant="secondary" onClick={() => setEditor(null)}>取消</Button>
              <Button disabled={saving} onClick={() => void saveEditor()}>{saving && <LoaderCircle size={15} className="spin" />}保存任务</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

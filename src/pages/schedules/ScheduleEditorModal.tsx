/** Schedule editor dialog (create/edit a scheduled export task). */

import { AlertCircle, LoaderCircle } from "lucide-react";

import { Overlay } from "../../components/Overlay";
import { Button, Field, Input, Switch } from "../../components/ui";
import {
  GroupMultiSelect,
  ProcessingOptionsEditor,
} from "../../components/TaskEditor";
import type {
  AppConfig,
  GroupInfo,
  ProcessingOptions,
  ScheduleDateMode,
  ScheduleDefinition,
  TaskGroup,
} from "../../types";
import { dateModeLabel, processingFrom, weekdays } from "./shared";

export interface ScheduleEditorModalProps {
  editor: ScheduleDefinition;
  isExisting: boolean;
  config: AppConfig;
  groups: GroupInfo[];
  loadingGroups: boolean;
  autoSyncAvailable: boolean;
  saving: boolean;
  onClose: () => void;
  onUpdate: (patch: Partial<ScheduleDefinition>) => void;
  onUpdateProcessing: (processing: ProcessingOptions) => void;
  onUpdateWeekday: (day: number) => void;
  onReloadGroups: () => void;
  onSave: () => void;
}

export function ScheduleEditorModal({
  editor,
  isExisting,
  config,
  groups,
  loadingGroups,
  autoSyncAvailable,
  saving,
  onClose,
  onUpdate,
  onUpdateProcessing,
  onUpdateWeekday,
  onReloadGroups,
  onSave,
}: ScheduleEditorModalProps) {
  return (
    <Overlay
      open
      onClose={onClose}
      closeDisabled={saving}
      size="lg"
      eyebrow="Schedule editor"
      title={isExisting ? "编辑定时任务" : "新建定时任务"}
      subtitle="保存后按设定的日期与时间自动执行；当前版本需要应用保持运行。"
      footer={(
        <>
          <span className="overlay-footer-note">到点后每个群的结果独立保存，不会互相覆盖。</span>
          <div className="overlay-footer-actions">
            <Button variant="secondary" onClick={onClose} disabled={saving}>取消</Button>
            <Button disabled={saving} onClick={onSave}>
              {saving && <LoaderCircle size={15} className="spin" />}保存任务
            </Button>
          </div>
        </>
      )}
    >
      <section className="schedule-editor-section">
        <h3>基本信息</h3>
        <div className="form-grid two-columns">
          <Field label="任务名称"><Input data-autofocus value={editor.name} onChange={(event) => onUpdate({ name: event.target.value })} /></Field>
          <Field label="每天执行时间"><Input type="time" value={editor.runAt} onChange={(event) => onUpdate({ runAt: event.target.value })} /></Field>
        </div>
        <Field label="每周执行日">
          <div className="weekday-picker">
            {weekdays.map((day) => <button type="button" key={day.value} className={editor.weekdays.includes(day.value) ? "selected" : ""} onClick={() => onUpdateWeekday(day.value)}>周{day.label}</button>)}
          </div>
        </Field>
      </section>

      <section className="schedule-editor-section">
        <h3>导出时间范围</h3>
        <div className="date-mode-picker">
          {(["today", "yesterday", "fixed"] as ScheduleDateMode[]).map((mode) => (
            <button type="button" key={mode} className={editor.dateMode === mode ? "selected" : ""} onClick={() => onUpdate({ dateMode: mode })}>
              <strong>{dateModeLabel[mode]}</strong>
              <small>{mode === "today" ? "每天动态取当天" : mode === "yesterday" ? "跨夜任务请选此项" : "始终导出指定日期"}</small>
            </button>
          ))}
        </div>
        <div className="form-grid schedule-range-grid">
          {editor.dateMode === "fixed" && <Field label="固定日期"><Input type="date" value={editor.fixedDate} onChange={(event) => onUpdate({ fixedDate: event.target.value })} /></Field>}
          <Field label="开始时间"><Input type="time" value={editor.startTime} onChange={(event) => onUpdate({ startTime: event.target.value })} /></Field>
          <Field label="结束时间" hint="早于开始时间时按次日处理"><Input type="time" value={editor.endTime} onChange={(event) => onUpdate({ endTime: event.target.value })} /></Field>
        </div>
      </section>

      <section className="schedule-editor-section">
        <div className="schedule-section-heading"><h3>执行群聊</h3><span>已选 {editor.groups.length} 个</span></div>
        <GroupMultiSelect groups={groups} selected={editor.groups} loading={loadingGroups} onReload={onReloadGroups} onChange={(next: TaskGroup[]) => onUpdate({ groups: next })} />
      </section>

      <section className="schedule-editor-section">
        <h3>处理方式</h3>
        <ProcessingOptionsEditor
          config={config}
          value={processingFrom(editor)}
          onChange={onUpdateProcessing}
          smartSheetHint={editor.autoSyncSmartSheet
            ? "任务完成后自动写入腾讯文档"
            : "默认仅生成待同步预览，由你确认后写入"}
        />
        <div className="schedule-auto-sync">
          <Switch
            checked={Boolean(editor.autoSyncSmartSheet)}
            disabled={!autoSyncAvailable}
            onChange={(autoSyncSmartSheet) => onUpdate({
              autoSyncSmartSheet: autoSyncSmartSheet && autoSyncAvailable,
            })}
            label="自动同步腾讯文档"
            description={autoSyncAvailable
              ? "无需手动确认；任务完成后直接写入已选模板"
              : "需先开启大模型分析和 Smart Sheet，并选择有效模板"}
          />
          <p className="schedule-auto-sync-risk">
            <AlertCircle size={14} />
            <span>这是外部写入操作。启用后定时任务到点会直接写入腾讯文档，不再等待人工确认；建议先手动执行一次并核对字段映射。</span>
          </p>
        </div>
      </section>

      <Switch checked={editor.enabled} onChange={(enabled) => onUpdate({ enabled })} label="保存后立即启用" description="关闭时仅保存任务配置" />
    </Overlay>
  );
}

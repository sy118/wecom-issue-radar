/** Schedule task list (including results whose schedule was deleted). */

import {
  CalendarClock,
  Clock3,
  CloudUpload,
  Edit3,
  LoaderCircle,
  Play,
  Plus,
  Power,
  Trash2,
  UsersRound,
} from "lucide-react";

import { Button, SectionHeader } from "../../components/ui";
import type { AppConfig, PendingScheduleSync, ScheduleDefinition } from "../../types";
import {
  dateModeLabel,
  pendingCountFrom,
  scheduleRangeLabel,
  weekdays,
  type OrphanPendingSyncGroup,
} from "./shared";

export interface ScheduleListProps {
  sortedSchedules: ScheduleDefinition[];
  orphanPendingGroups: OrphanPendingSyncGroup[];
  pendingBySchedule: Map<string, PendingScheduleSync[]>;
  activity: Record<string, string>;
  saving: boolean;
  runningId: string;
  syncing: boolean;
  refreshingConfirmation: boolean;
  config: AppConfig;
  onCreate: () => void;
  onToggle: (schedule: ScheduleDefinition) => void;
  onRunNow: (schedule: ScheduleDefinition) => void;
  onEdit: (schedule: ScheduleDefinition) => void;
  onRemove: (schedule: ScheduleDefinition) => void;
  onOpenPending: (scheduleId: string) => void;
  onDiscardPending: (items: PendingScheduleSync[], scheduleName: string) => void;
}

export function ScheduleList({
  sortedSchedules,
  orphanPendingGroups,
  pendingBySchedule,
  activity,
  saving,
  runningId,
  syncing,
  refreshingConfirmation,
  config,
  onCreate,
  onToggle,
  onRunNow,
  onEdit,
  onRemove,
  onOpenPending,
  onDiscardPending,
}: ScheduleListProps) {
  return (
    <>
      {orphanPendingGroups.length > 0 && (
        <section className="glass-card schedule-list-card">
          <SectionHeader
            title="已删除任务的待确认结果"
            description="任务可能在运行期间被删除；本地结果仍完整保留，需要你确认写入或主动放弃。"
          />
          <div className="schedule-list">
            {orphanPendingGroups.map((group) => {
              const createdAt = group.latestCreatedAt ? new Date(group.latestCreatedAt) : null;
              const createdAtLabel = createdAt && !Number.isNaN(createdAt.getTime())
                ? createdAt.toLocaleString("zh-CN", { hour12: false })
                : "完成时间未知";
              return (
                <article className="schedule-row" key={group.scheduleId}>
                  <div className="schedule-power"><CloudUpload size={15} /></div>
                  <div className="schedule-time"><strong>{group.pendingCount}</strong><small>条待写入</small></div>
                  <div className="schedule-main">
                    <strong>{group.scheduleName}</strong>
                    <div className="schedule-meta">
                      <span>{group.items.length} 次待确认运行</span>
                      <span><Clock3 size={12} />{createdAtLabel}</span>
                    </div>
                  </div>
                  <div className="schedule-actions">
                    <Button variant="secondary" disabled={syncing || refreshingConfirmation} onClick={() => onOpenPending(group.scheduleId)}>
                      <CloudUpload size={14} />确认同步
                    </Button>
                    <Button variant="danger" disabled={syncing} onClick={() => onDiscardPending(group.items, group.scheduleName)}>
                      放弃
                    </Button>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}

      <section className="glass-card schedule-list-card">
        <SectionHeader title="任务列表" description="开关关闭后会保留配置，但不会自动执行。" />
        {sortedSchedules.length ? (
          <div className="schedule-list">
            {sortedSchedules.map((schedule) => {
              const schedulePendingSyncs = pendingBySchedule.get(schedule.id) ?? [];
              const schedulePendingCount = pendingCountFrom(schedulePendingSyncs);
              return (
                <article className={schedule.enabled ? "schedule-row" : "schedule-row disabled"} key={schedule.id}>
                <button
                  type="button"
                  className={schedule.enabled ? "schedule-power enabled" : "schedule-power"}
                  title={schedule.enabled ? "暂停任务" : "启用任务"}
                  disabled={saving}
                  onClick={() => onToggle(schedule)}
                >
                  <Power size={15} />
                </button>
                <div className="schedule-time"><strong>{schedule.runAt}</strong><small>{schedule.enabled ? "已启用" : "已暂停"}</small></div>
                <div className="schedule-main">
                  <strong>{schedule.name}</strong>
                  <div className="schedule-meta">
                    <span><UsersRound size={12} />{schedule.groups.length} 个群</span>
                    <span><Clock3 size={12} />{scheduleRangeLabel(schedule)}</span>
                    <span>{dateModeLabel[schedule.dateMode]}</span>
                    {schedule.prepareSmartSheet && <span>{config.smart_sheet.templates.find((template) => template.id === schedule.smartSheetTemplateId)?.name ?? "腾讯模板待确认"}</span>}
                    <span>周{schedule.weekdays.map((day) => weekdays.find((item) => item.value === day)?.label).join("、")}</span>
                  </div>
                  {activity[schedule.id] && <p className="schedule-activity">{activity[schedule.id]}</p>}
                </div>
                <div className="schedule-actions">
                  {schedulePendingCount > 0 && (
                    <Button
                      className="schedule-confirm-sync"
                      variant="secondary"
                      disabled={syncing || refreshingConfirmation}
                      onClick={() => onOpenPending(schedule.id)}
                    >
                      <CloudUpload size={14} />确认同步 {schedulePendingCount}
                    </Button>
                  )}
                  <Button variant="secondary" disabled={runningId === schedule.id} onClick={() => onRunNow(schedule)}>
                    {runningId === schedule.id ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}立即执行
                  </Button>
                  <Button variant="ghost" title="编辑" onClick={() => onEdit(schedule)}><Edit3 size={15} /></Button>
                  <Button variant="ghost" title="删除" disabled={runningId === schedule.id} onClick={() => onRemove(schedule)}><Trash2 size={15} /></Button>
                </div>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="schedule-empty">
            <div><CalendarClock size={27} /></div>
            <strong>还没有定时任务</strong>
            <p>创建后，应用会在指定日期和时间自动导出群聊。</p>
            <Button variant="secondary" onClick={onCreate}><Plus size={15} />创建第一个任务</Button>
          </div>
        )}
      </section>
    </>
  );
}

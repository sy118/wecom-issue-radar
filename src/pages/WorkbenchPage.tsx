import { useEffect, useState } from "react";
import {
  ArrowRight,
  BellRing,
  CalendarClock,
  CheckCircle2,
  Circle,
  Download,
  LoaderCircle,
  Play,
  RefreshCw,
  Settings2,
} from "lucide-react";
import { bridge } from "../lib/bridge";
import { progressSummary, type OnboardingProgress } from "../lib/onboarding";
import type {
  AppConfig,
  PageId,
  PendingScheduleSync,
  ReplyRuntimeSnapshot,
  ScheduleExecutionHistoryItem,
} from "../types";
import { Button } from "../components/ui";

export function WorkbenchPage({
  config,
  onboardingCompleted,
  onboardingProgress,
  onOpenOnboarding,
  onNavigate,
}: {
  config: AppConfig;
  onboardingCompleted: boolean;
  onboardingProgress?: OnboardingProgress;
  onOpenOnboarding: () => void;
  onNavigate: (page: PageId) => void;
}) {
  const [pending, setPending] = useState<PendingScheduleSync[]>([]);
  const [history, setHistory] = useState<ScheduleExecutionHistoryItem[]>([]);
  const [snapshot, setSnapshot] = useState<ReplyRuntimeSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = async () => {
    setLoading(true);
    setError("");
    const results = await Promise.allSettled([
      bridge.listPendingSmartSheetSyncs(),
      bridge.listScheduleExecutionHistory(1, 5),
      bridge.replyRuntimeQuery<ReplyRuntimeSnapshot>({
        protocolVersion: 1,
        body: { kind: "runtime.snapshot" },
      }),
    ]);
    const [pendingResult, historyResult, snapshotResult] = results;
    if (pendingResult.status === "fulfilled") setPending(pendingResult.value);
    if (historyResult.status === "fulfilled") setHistory(historyResult.value.items);
    if (snapshotResult.status === "fulfilled") setSnapshot(snapshotResult.value);
    if (results.every((item) => item.status === "rejected")) {
      setError("后台引擎暂时不可用，请确认已安装完整应用并重试。");
    }
    setLoading(false);
  };

  useEffect(() => {
    void refresh();
  }, []);

  const activeListeners = snapshot?.listeners?.filter((item) => item.enabled).length ?? 0;
  const pendingWorks = snapshot?.pendingCount ?? 0;
  const targets = config.publish_targets ?? [];
  const checks = [
    { label: "企业微信数据目录", done: Boolean(String(config.wxwork_db_dir || "").trim()), page: "settings" as PageId },
    { label: "数据库密钥文件", done: Boolean(String(config.wxwork_keys_file || "").trim()), page: "settings" as PageId },
    { label: "大模型连接", done: Boolean(config.llm?.base_url && config.llm?.model), page: "settings" as PageId },
    { label: "发布目标", done: targets.length > 0, page: "strategy-publish" as PageId },
  ];

  return (
    <div className="workbench-page">
      <div className="workbench-hero">
        <div>
          <h1>今天想处理哪件事？</h1>
          <p>
            导出群聊问题、让 AI 在群里拟稿、或者先配置好数据源。第一次使用建议从引导开始，三分钟就能跑通一件事。
          </p>
        </div>
        <div className="workbench-card-actions">
          <Button onClick={() => onNavigate("wecom-issue")}>
            <Play size={16} /> 开始处理
          </Button>
          <Button variant="secondary" onClick={() => onNavigate("settings")}>
            <Settings2 size={16} /> 配置
          </Button>
        </div>
      </div>

      {!onboardingCompleted && (
        <div className="workbench-banner">
          <span>
            还没完成新手引导：{onboardingProgress ? progressSummary(onboardingProgress) : "先选一条路径"}。三分钟把最常用的一件事跑通。
          </span>
          <Button variant="secondary" onClick={onOpenOnboarding}>
            继续设置 <ArrowRight size={15} />
          </Button>
        </div>
      )}

      {error && <div className="workbench-banner">{error}</div>}

      <div className="workbench-grid">
        <section className="workbench-card">
          <h2>群监听</h2>
          <div className="workbench-metric">
            <strong>{activeListeners}</strong>
            <span>个启用中的监听</span>
          </div>
          <p>
            {snapshot?.running
              ? `运行中 · 待处理 ${pendingWorks} 条`
              : "监听未运行；打开群监听回复页后会自动启动。"}
          </p>
          <div className="workbench-card-actions">
            <Button variant="secondary" onClick={() => onNavigate("wecom-reply")}>
              查看监听 <ArrowRight size={15} />
            </Button>
          </div>
        </section>

        <section className="workbench-card">
          <h2>待确认与待同步</h2>
          <div className="workbench-metric">
            <strong>{pending.length}</strong>
            <span>个腾讯文档同步待确认</span>
          </div>
          <p>{pending.length > 0 ? "上一次任务有结果等待你确认写入。" : "没有待确认的外部写入。"}</p>
          <div className="workbench-card-actions">
            <Button variant="secondary" onClick={() => onNavigate("wecom-issue")}>
              去处理 <ArrowRight size={15} />
            </Button>
          </div>
        </section>

        <section className="workbench-card">
          <h2>配置检查</h2>
          <ul className="workbench-checklist">
            {checks.map((item) => (
              <li key={item.label}>
                {item.done ? (
                  <CheckCircle2 size={17} className="done" />
                ) : (
                  <Circle size={17} />
                )}
                <button
                  type="button"
                  className="button button-ghost"
                  style={{ padding: 0, minHeight: 0, fontSize: "inherit" }}
                  onClick={() => onNavigate(item.page)}
                >
                  {item.label}
                  {!item.done && " · 去配置"}
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section className="workbench-card">
          <h2>最近定时任务</h2>
          {loading && <p><LoaderCircle className="spin" size={16} /> 正在读取…</p>}
          {!loading && history.length === 0 && <p>还没有执行记录。创建定时任务后这里会显示结果。</p>}
          {!loading && history.length > 0 && (
            <ul className="workbench-recent">
              {history.map((item) => (
                <li key={item.executionId}>
                  <span>{item.scheduleName}</span>
                  <small>
                    {item.status === "success"
                      ? "成功"
                      : item.status === "partial"
                        ? "部分完成"
                        : item.status === "empty"
                          ? "无记录"
                          : "失败"}
                    {" · "}
                    {item.startedAt?.slice(5, 16).replace("T", " ")}
                  </small>
                </li>
              ))}
            </ul>
          )}
          <div className="workbench-card-actions">
            <Button variant="secondary" onClick={() => onNavigate("wecom-schedules")}>
              <CalendarClock size={15} /> 定时任务
            </Button>
            <Button variant="ghost" onClick={() => void refresh()}>
              <RefreshCw size={15} /> 刷新
            </Button>
          </div>
        </section>

        <section className="workbench-card">
          <h2>快捷入口</h2>
          <p>常用的几个动作，一步直达。</p>
          <div className="workbench-card-actions">
            <Button variant="secondary" onClick={() => onNavigate("wecom-issue")}>
              <Download size={15} /> 导出群聊
            </Button>
            <Button variant="secondary" onClick={() => onNavigate("wecom-reply")}>
              <BellRing size={15} /> 群监听
            </Button>
            <Button variant="secondary" onClick={() => onNavigate("strategy-publish")}>
              发布目标
            </Button>
          </div>
        </section>
      </div>
    </div>
  );
}

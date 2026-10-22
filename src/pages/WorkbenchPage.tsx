import { useEffect, useState } from "react";
import {
  ArrowRight,
  BellRing,
  CalendarClock,
  CheckCircle2,
  ChevronRight,
  Circle,
  Download,
  LoaderCircle,
  Play,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { bridge } from "../lib/bridge";
import type {
  AppConfig,
  PageId,
  PendingScheduleSync,
  ReplyRuntimeSnapshot,
  ScheduleExecutionHistoryItem,
} from "../types";
import { Button } from "../components/ui";
import { progressSummary, type OnboardingProgress } from "../lib/onboarding";

function relativeTime(iso: string | undefined): string {
  if (!iso) return "";
  const timestamp = new Date(iso).getTime();
  if (!Number.isFinite(timestamp)) return "";
  const minutes = Math.max(0, Math.round((Date.now() - timestamp) / 60000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.round(hours / 24)} 天前`;
}

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
      bridge.listScheduleExecutionHistory(1, 4),
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
  const readyCount = checks.filter((item) => item.done).length;

  return (
    <div className="page-content wb-page">
      <section className="wb-hero">
        <div className="wb-hero-copy">
          <span className="wb-kicker">
            <Sparkles size={14} /> 本地智能工作台
          </span>
          <h1>今天想处理哪件事？</h1>
          <p>
            导出群聊问题、让 AI 在群里拟稿，或者先配置好数据源。第一次使用建议从引导开始，三分钟就能跑通一件事。
          </p>
          <div className="wb-hero-actions">
            <Button onClick={() => onNavigate("wecom-issue")}>
              <Play size={16} /> 开始处理
            </Button>
            <Button variant="secondary" onClick={() => onNavigate("settings")}>
              <Settings2 size={16} /> 配置数据源
            </Button>
          </div>
          <span className="wb-hero-note">
            <ShieldCheck size={14} /> 聊天内容、截图与模型凭据只在本机处理
          </span>
        </div>
        <div className="wb-hero-stats">
          <button type="button" className="wb-stat" onClick={() => onNavigate("wecom-reply")}>
            <span className="wb-stat-label">启用监听</span>
            <strong>{activeListeners}</strong>
            <small>个群监听</small>
          </button>
          <button type="button" className="wb-stat" onClick={() => onNavigate("wecom-reply")}>
            <span className="wb-stat-label">待处理</span>
            <strong>{pendingWorks}</strong>
            <small>个群内问题</small>
          </button>
          <button type="button" className="wb-stat" onClick={() => onNavigate("wecom-issue")}>
            <span className="wb-stat-label">待同步</span>
            <strong>{pending.length}</strong>
            <small>条文档写入</small>
          </button>
        </div>
      </section>

      {!onboardingCompleted && (
        <div className="wb-banner">
          <div>
            <strong>还没完成新手引导</strong>
            <span>{onboardingProgress ? `${progressSummary(onboardingProgress)}。` : ""}先选一条路径，把最常用的一件事跑通。</span>
          </div>
          <Button variant="secondary" onClick={onOpenOnboarding}>
            继续设置 <ArrowRight size={15} />
          </Button>
        </div>
      )}

      {error && <div className="wb-banner wb-banner-error">{error}</div>}

      <div className="wb-grid">
        <section className="wb-card wb-checks">
          <header className="wb-card-head">
            <div>
              <h2>配置检查</h2>
              <p>这四项就绪后，绝大多数功能都能直接用。</p>
            </div>
            <span className={`wb-pill ${readyCount === checks.length ? "is-success" : "is-warning"}`}>
              {readyCount}/{checks.length} 已就绪
            </span>
          </header>
          <ul className="wb-check-list">
            {checks.map((item) => (
              <li key={item.label}>
                <button type="button" className="wb-check-row" onClick={() => onNavigate(item.page)}>
                  <span className={`wb-check-icon ${item.done ? "is-done" : ""}`}>
                    {item.done ? <CheckCircle2 size={17} /> : <Circle size={17} />}
                  </span>
                  <span className="wb-check-copy">
                    <strong>{item.label}</strong>
                    <small>{item.done ? "已配置" : "尚未配置，点此完善"}</small>
                  </span>
                  <ChevronRight size={16} className="wb-check-arrow" />
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section className="wb-card wb-listeners">
          <header className="wb-card-head">
            <div>
              <h2>群监听与同步</h2>
              <p>自动拟稿只在你确认后才发送。</p>
            </div>
            <span className={`status-dot ${snapshot?.running ? "" : "is-off"}`} />
          </header>
          <div className="wb-kv">
            <span>运行状态</span>
            <strong>{snapshot?.running ? "监听运行中" : "监听未启动"}</strong>
          </div>
          <div className="wb-kv">
            <span>正在处理</span>
            <strong>{snapshot?.activeRetrievals ?? 0} 个问题</strong>
          </div>
          <div className="wb-kv">
            <span>待确认同步</span>
            <strong>{pending.length} 条</strong>
          </div>
          <div className="wb-card-actions">
            <Button variant="secondary" onClick={() => onNavigate("wecom-reply")}>
              <BellRing size={15} /> 查看监听
            </Button>
            <Button variant="ghost" onClick={() => onNavigate("wecom-issue")}>
              去处理同步
            </Button>
          </div>
        </section>

        <section className="wb-card wb-recent">
          <header className="wb-card-head">
            <div>
              <h2>最近定时任务</h2>
              <p>应用关闭后定时任务不会执行。</p>
            </div>
            <button type="button" className="wb-icon-button" title="刷新" onClick={() => void refresh()}>
              <RefreshCw size={15} className={loading ? "spin" : undefined} />
            </button>
          </header>
          {loading && (
            <div className="wb-empty">
              <LoaderCircle className="spin" size={18} /> 正在读取…
            </div>
          )}
          {!loading && history.length === 0 && (
            <div className="wb-empty">还没有执行记录。创建定时任务后这里会显示结果。</div>
          )}
          {!loading && history.length > 0 && (
            <ul className="wb-recent-list">
              {history.map((item) => (
                <li key={item.executionId}>
                  <span className={`wb-dot ${item.status === "success" ? "is-success" : item.status === "failed" ? "is-danger" : "is-warning"}`} />
                  <span className="wb-recent-copy">
                    <strong>{item.scheduleName}</strong>
                    <small>{relativeTime(item.startedAt)} · {item.status === "success" ? "成功" : item.status === "partial" ? "部分完成" : item.status === "empty" ? "无记录" : "失败"}</small>
                  </span>
                </li>
              ))}
            </ul>
          )}
          <div className="wb-card-actions">
            <Button variant="secondary" onClick={() => onNavigate("wecom-schedules")}>
              <CalendarClock size={15} /> 管理定时任务
            </Button>
          </div>
        </section>

        <section className="wb-card wb-quick">
          <header className="wb-card-head">
            <div>
              <h2>快捷入口</h2>
              <p>常用的几个动作，一步直达。</p>
            </div>
          </header>
          <div className="wb-quick-grid">
            <button type="button" className="wb-quick-item" onClick={() => onNavigate("wecom-issue")}>
              <span className="wb-quick-icon is-blue"><Download size={18} /></span>
              <strong>导出群聊</strong>
              <small>选群 + 时间段，生成 Excel / Markdown</small>
            </button>
            <button type="button" className="wb-quick-item" onClick={() => onNavigate("wecom-reply")}>
              <span className="wb-quick-icon is-emerald"><BellRing size={18} /></span>
              <strong>群监听</strong>
              <small>发现问题后自动拟稿，你确认再发</small>
            </button>
            <button type="button" className="wb-quick-item" onClick={() => onNavigate("strategy-publish")}>
              <span className="wb-quick-icon is-amber"><Settings2 size={18} /></span>
              <strong>发布目标</strong>
              <small>Excel / Markdown / 腾讯文档自由组合</small>
            </button>
            <button type="button" className="wb-quick-item" onClick={() => onNavigate("daily-reply")}>
              <span className="wb-quick-icon is-violet"><Sparkles size={18} /></span>
              <strong>回复助手</strong>
              <small>潜台词解读、三版回复与发送前检查</small>
            </button>
          </div>
        </section>
      </div>
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import {
  CalendarRange,
  Copy,
  Download,
  FileText,
  ListTodo,
  LoaderCircle,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { toast } from "sonner";

import { Button, Field, Input, SectionHeader } from "../../components/ui";
import { toUserErrorMessage } from "../../lib/errors";
import { generateWeeklyReport } from "../../components/daily/api";
import {
  composeWeeklyMarkdown,
  dateRangeLabel,
  lastNDaysRange,
  lastWeekRange,
  validateDateRange,
  weekRange,
  type DateRange,
} from "../../components/daily/logic";
import type { WeeklyResult } from "../../components/daily/types";
import { TodosPanel } from "../../components/daily/TodosPanel";
import {
  DailyError,
  DailyNote,
  DailyPageHeader,
  StatGrid,
  copyText,
  downloadMarkdown,
} from "../../components/daily/ui";

const SOURCE_OPTIONS = [
  { value: "wecom", label: "企业微信" },
  { value: "wechat", label: "个人微信" },
];

type ReportTab = "report" | "todos";

export function DailyReportPage() {
  const initial = weekRange();
  const [tab, setTab] = useState<ReportTab>("report");
  const [startDate, setStartDate] = useState(initial.startDate);
  const [endDate, setEndDate] = useState(initial.endDate);
  const [sources, setSources] = useState<string[]>(["wecom", "wechat"]);
  const [result, setResult] = useState<WeeklyResult | null>(null);
  const [draft, setDraft] = useState("");
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");

  const applyRange = useCallback((range: DateRange) => {
    setStartDate(range.startDate);
    setEndDate(range.endDate);
  }, []);

  const toggleSource = (value: string) => {
    setSources((current) =>
      current.includes(value) ? current.filter((entry) => entry !== value) : [...current, value],
    );
  };

  const handleGenerate = async () => {
    const validation = validateDateRange(startDate, endDate);
    if (validation) {
      toast.warning(validation);
      return;
    }
    setGenerating(true);
    setError("");
    try {
      const response = await generateWeeklyReport(startDate, endDate, sources);
      setResult(response);
      setDraft(response.draft ?? "");
    } catch (reason) {
      setError(toUserErrorMessage(reason, "生成周报失败，请稍后重试。"));
    } finally {
      setGenerating(false);
    }
  };

  const handleCopy = async () => {
    if (!draft.trim()) {
      toast.warning("草稿还是空的，先生成或手动写一点内容");
      return;
    }
    const copied = await copyText(draft);
    if (copied) toast.success("周报已复制");
    else toast.error("复制失败，请手动选择文本复制");
  };

  const handleExport = async () => {
    if (!draft.trim()) {
      toast.warning("草稿还是空的，先生成或手动写一点内容");
      return;
    }
    const markdown = composeWeeklyMarkdown({
      startDate,
      endDate,
      draft,
      stats: result?.stats ?? {},
    });
    const ok = downloadMarkdown(`weekly-${startDate}-${endDate}.md`, markdown);
    if (ok) {
      toast.success("已导出 Markdown 文件", { description: "浏览器会保存到默认下载目录。" });
      return;
    }
    const copied = await copyText(markdown);
    toast.info(copied ? "无法直接下载，已复制 Markdown" : "导出失败，请手动复制草稿");
  };

  return (
    <div className="page-content daily-page">
      <DailyPageHeader
        eyebrow="Daily Report"
        icon={<FileText size={13} />}
        title="周报总结"
        description="把一周的群聊和个人微信消息整理成可编辑的周报草稿，支持复制或导出 Markdown。"
        actions={
          <Button variant="secondary" onClick={() => void handleGenerate()} disabled={generating}>
            {generating ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}
            重新生成
          </Button>
        }
      />

      <div className="daily-tabs" role="tablist" aria-label="周报与待办">
        <button type="button" role="tab" aria-selected={tab === "report"} onClick={() => setTab("report")}>
          <FileText size={15} /> 周报草稿
        </button>
        <button type="button" role="tab" aria-selected={tab === "todos"} onClick={() => setTab("todos")}>
          <ListTodo size={15} /> 待办
        </button>
      </div>

      {tab === "todos" ? (
        <section className="glass-card">
          <TodosPanel />
        </section>
      ) : (
        <>
          <section className="glass-card">
            <SectionHeader
              title="选择范围"
              description="默认统计本周（周一到今天）；也可以自定义日期，只统计你关心的来源。"
            />
            <div className="daily-range-actions">
              <Button variant="secondary" onClick={() => applyRange(weekRange())}>
                本周
              </Button>
              <Button variant="secondary" onClick={() => applyRange(lastWeekRange())}>
                上周
              </Button>
              <Button variant="secondary" onClick={() => applyRange(lastNDaysRange(new Date(), 7))}>
                最近 7 天
              </Button>
              <span className="daily-hint">{dateRangeLabel({ startDate, endDate })}</span>
            </div>

            <div className="daily-filter-row">
              <Field label="开始日期">
                <Input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
              </Field>
              <Field label="结束日期">
                <Input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
              </Field>
              <div className="field">
                <span className="field-label">来源</span>
                <div className="daily-sources">
                  {SOURCE_OPTIONS.map((option) => (
                    <label key={option.value}>
                      <input
                        type="checkbox"
                        checked={sources.includes(option.value)}
                        onChange={() => toggleSource(option.value)}
                      />
                      {option.label}
                    </label>
                  ))}
                </div>
              </div>
              <div className="daily-toolbar-actions">
                <Button onClick={() => void handleGenerate()} disabled={generating}>
                  {generating ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}
                  {generating ? "正在生成…" : "生成周报"}
                </Button>
              </div>
            </div>
          </section>

          {error && <DailyError message={error} onRetry={() => void handleGenerate()} />}

          {result?.fallback && (
            <div className="daily-warning">
              <CalendarRange size={16} />
              <span>AI 暂不可用，已按统计生成基础草稿；可以直接编辑，稍后再试一次可能更完整。</span>
            </div>
          )}

          <section className="glass-card">
            <SectionHeader
              title="周报草稿"
              description="生成后可以直接修改；导出时会把统计摘要一并写进 Markdown。"
              action={
                <div className="daily-actions">
                  <Button variant="secondary" onClick={() => void handleCopy()}>
                    <Copy size={15} />复制
                  </Button>
                  <Button variant="secondary" onClick={() => void handleExport()}>
                    <Download size={15} />导出 Markdown
                  </Button>
                </div>
              }
            />
            {result && result.stats && (
              <div style={{ marginBottom: "var(--space-3)" }}>
                <StatGrid stats={result.stats} />
              </div>
            )}
            <textarea
              className="textarea daily-report-textarea"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="点击「生成周报」后，这里会出现可编辑的草稿；也可以直接手写。"
              aria-label="周报草稿"
            />
          </section>

          <DailyNote>周报默认在本机生成；只有你自己复制、导出或粘贴到外部工具时，内容才会离开本机。</DailyNote>
        </>
      )}
    </div>
  );
}

import { useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { Copy, Eye, ListChecks, Plug, Plus, Save, Trash2 } from "lucide-react";
import { toast } from "sonner";
import type { AppConfig, PublishTarget, PublishTargetKind } from "../types";
import { useUnsavedChanges } from "../lib/unsaved";
import { toUserErrorMessage } from "../lib/errors";
import { Button, Field, Input, SectionHeader } from "../components/ui";
import { ConnectionTab } from "./publish/ConnectionTab";
import { MappingTab } from "./publish/MappingTab";
import { PreviewTab } from "./publish/PreviewTab";
import {
  buildPublishSave,
  duplicateTemplateIds,
  errorCount,
  issueAnalysisPromptOptions,
  mappingSourceOptions,
  normalizeUpload,
  previewDraft,
  problemsForTarget,
  readTencentDraft,
  targetConfigFromDraft,
  targetsFromConfig,
  validateDraft,
} from "./publish/logic";
import type { TencentDraft, UploadDraft } from "./publish/types";
import "./publish/publish.css";

const KIND_LABEL: Record<PublishTargetKind, string> = {
  excel: "Excel 文件",
  markdown: "Markdown 文件",
  tencent_smartsheet: "腾讯文档 Smart Sheet",
  feishu_doc: "飞书文档（预留）",
};

type TabId = "connection" | "mapping" | "preview";

const TABS: Array<{ id: TabId; label: string; icon: ReactNode }> = [
  { id: "connection", label: "连接", icon: <Plug size={15} /> },
  { id: "mapping", label: "字段映射", icon: <ListChecks size={15} /> },
  { id: "preview", label: "预览", icon: <Eye size={15} /> },
];

function cloneTargetConfig(config: Record<string, unknown>): Record<string, unknown> {
  try {
    return JSON.parse(JSON.stringify(config)) as Record<string, unknown>;
  } catch {
    return { ...config };
  }
}

function nextId(targets: PublishTarget[], kind: PublishTargetKind): string {
  let index = targets.length + 1;
  let id = kind === "tencent_smartsheet" ? `tencent:${index}` : `${kind}_${index}`;
  const existing = new Set(targets.map((item) => item.id));
  while (existing.has(id)) {
    index += 1;
    id = kind === "tencent_smartsheet" ? `tencent:${index}` : `${kind}_${index}`;
  }
  return id;
}

export function PublishTargetsPage({
  config,
  onSave,
}: {
  config: AppConfig;
  onSave: (config: AppConfig) => Promise<void>;
}) {
  const initial = useMemo(() => targetsFromConfig(config), [config]);
  const [targets, setTargets] = useState<PublishTarget[]>(initial);
  const [selectedId, setSelectedId] = useState(initial[0]?.id ?? "");
  const [tab, setTab] = useState<TabId>("connection");
  const [upload, setUpload] = useState<UploadDraft>(() => normalizeUpload(config.smart_sheet?.upload));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  const selected = targets.find((item) => item.id === selectedId) ?? targets[0];
  useUnsavedChanges("publish-targets", dirty);

  const tabIndex = TABS.findIndex((item) => item.id === tab);
  const selectTab = (next: TabId) => setTab(next);
  // Roving tablist: Left/Right/Home/End move between the three tabs and keep
  // keyboard focus on the newly selected tab.
  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const current = Math.max(0, tabIndex);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (current + 1) % TABS.length;
    else if (event.key === "ArrowLeft") nextIndex = (current - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = TABS.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const next = TABS[nextIndex];
    setTab(next.id);
    window.requestAnimationFrame(() => {
      document.getElementById(`pub-tab-${next.id}`)?.focus();
    });
  };

  const draft = useMemo<TencentDraft | null>(
    () => (selected?.kind === "tencent_smartsheet" ? readTencentDraft(config, selected) : null),
    [config, selected],
  );
  const prompts = useMemo(() => issueAnalysisPromptOptions(config), [config]);
  const sources = useMemo(
    () => mappingSourceOptions(config, draft?.prompt_id),
    [config, draft?.prompt_id],
  );
  const problems = useMemo(
    () => (draft ? validateDraft(draft, sources) : []),
    [draft, sources],
  );
  const preview = useMemo(
    () => (draft ? previewDraft(draft, sources) : null),
    [draft, sources],
  );
  const promptName = prompts.find((prompt) => prompt.id === draft?.prompt_id)?.name ?? "";
  const targetErrors = useMemo(() => Object.fromEntries(targets.map((target) => [
    target.id,
    errorCount(problemsForTarget(config, target)),
  ])), [config, targets]);
  const totalErrors = targets.reduce((sum, target) => sum + (targetErrors[target.id] ?? 0), 0);

  const updateSelected = (patch: Partial<PublishTarget>) => {
    if (!selected) return;
    setTargets((current) => current.map((item) => (
      item.id === selected.id ? { ...item, ...patch } : item
    )));
    setDirty(true);
  };

  const applyDraft = (next: TencentDraft) => {
    if (!selected) return;
    updateSelected({
      config: targetConfigFromDraft(next, selected.config, config, selected.id),
    });
  };

  const changePrompt = (promptId: string) => {
    if (!draft) return;
    applyDraft({ ...draft, prompt_id: promptId });
    const nextSources = mappingSourceOptions(config, promptId);
    const validKeys = new Set(nextSources.map((source) => source.key));
    const invalid = draft.field_mappings.filter((mapping) => !validKeys.has(mapping.source_key)).length;
    if (invalid > 0) {
      toast.info(`已切换提示词，保留 ${draft.field_mappings.length} 条映射`, {
        description: `${invalid} 条映射的来源字段不在新提示词中，已标黄；可改选来源或切回原提示词。`,
      });
    }
  };

  const addTarget = (kind: PublishTargetKind) => {
    const id = nextId(targets, kind);
    const target: PublishTarget = {
      id,
      name: KIND_LABEL[kind],
      kind,
      config: kind === "tencent_smartsheet"
        ? { template_id: id.slice("tencent:".length), batch_size: 50, schema: {}, field_mappings: [], transforms: {} }
        : {},
    };
    setTargets((current) => [...current, target]);
    setSelectedId(id);
    setTab("connection");
    setDirty(true);
  };

  const duplicate = () => {
    if (!selected) return;
    const id = nextId(targets, selected.kind);
    let configCopy = cloneTargetConfig(selected.config);
    if (selected.kind === "tencent_smartsheet") {
      const used = new Set(targets
        .filter((target) => target.kind === "tencent_smartsheet")
        .map((target) => readTencentDraft(config, target).template_id));
      const base = String(configCopy.template_id ?? "").trim() || "template";
      let templateId = `${base}_copy`;
      let suffix = 2;
      while (used.has(templateId)) {
        templateId = `${base}_copy_${suffix}`;
        suffix += 1;
      }
      configCopy = { ...configCopy, template_id: templateId };
    }
    const copy: PublishTarget = {
      ...selected,
      id,
      name: `${selected.name} 副本`,
      config: configCopy,
    };
    setTargets((current) => [...current, copy]);
    setSelectedId(id);
    setDirty(true);
  };

  const remove = () => {
    if (!selected) return;
    if (selected.kind === "excel" || selected.kind === "markdown") return;
    const next = targets.filter((item) => item.id !== selected.id);
    setTargets(next);
    setSelectedId(next[0]?.id ?? "");
    setDirty(true);
  };

  const save = async () => {
    const duplicateTemplateIdsFound = duplicateTemplateIds(config, targets);
    if (duplicateTemplateIdsFound.length > 0) {
      toast.error("模板 ID 重复，暂不能保存", {
        description: `多个腾讯文档目标使用了同一个模板 ID：${duplicateTemplateIdsFound.join("、")}`,
      });
      return;
    }
    const invalid = targets.filter((target) => (targetErrors[target.id] ?? 0) > 0);
    if (invalid.length > 0) {
      const first = invalid[0];
      const firstProblem = problemsForTarget(config, first).find((problem) => problem.level === "error");
      toast.error(`还有 ${totalErrors} 个校验错误，暂不能保存`, {
        description: `${first.name}：${firstProblem?.message ?? "请检查字段映射"}`,
      });
      return;
    }
    setSaving(true);
    try {
      const built = buildPublishSave(config, targets, upload);
      await onSave(built.config);
      setTargets(built.targets);
      setDirty(false);
      toast.success("发布目标已保存", {
        description: "已同步写入旧版 Smart Sheet 模板镜像，现有管线与定时任务无需修改。",
      });
    } catch (error) {
      toast.error("发布目标保存失败", { description: toUserErrorMessage(error) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page-content publish-page">
      <div className="page-title-row">
        <div>
          <span className="eyebrow">策略与发布</span>
          <h1>发布目标</h1>
          <p>决定任务结果写到哪里。提示词负责“怎么整理”，发布目标负责“写到哪里”，两者可以自由组合。</p>
        </div>
        <div className="workbench-card-actions">
          <Button variant="secondary" onClick={() => addTarget("tencent_smartsheet")}>
            <Plus size={15} /> 腾讯文档
          </Button>
          <Button variant="secondary" onClick={() => addTarget("markdown")}>
            <Plus size={15} /> Markdown
          </Button>
          <Button onClick={() => void save()} disabled={!dirty || saving}>
            <Save size={15} />
            {saving
              ? "保存中…"
              : !dirty
                ? "已保存"
                : totalErrors > 0
                  ? `保存（${totalErrors} 个错误）`
                  : "保存"}
          </Button>
        </div>
      </div>

      <div className="publish-layout">
        <div className="publish-list" role="group" aria-label="发布目标列表">
          {targets.map((target) => {
            const errors = targetErrors[target.id] ?? 0;
            const active = target.id === selected?.id;
            return (
              <button
                key={target.id}
                type="button"
                className={`publish-item ${active ? "active" : ""}`}
                aria-current={active ? "true" : undefined}
                onClick={() => setSelectedId(target.id)}
              >
                <span className="publish-item-text">
                  <strong>{target.name}</strong>
                  <span className="publish-item-kind">{KIND_LABEL[target.kind]}</span>
                </span>
                {target.kind === "tencent_smartsheet" && errors > 0 && (
                  <span className="publish-item-errors">{errors} 个错误</span>
                )}
              </button>
            );
          })}
        </div>

        {selected ? (
          <section className="publish-editor" aria-label="发布目标配置">
            <SectionHeader
              title={selected.name || KIND_LABEL[selected.kind]}
              description={`${KIND_LABEL[selected.kind]} · 同一个提示词可以同时导出 Excel/Markdown，也可以确认后写入腾讯文档。`}
              action={
                <div className="workbench-card-actions">
                  <Button variant="ghost" onClick={duplicate} aria-label="复制发布目标" title="复制发布目标">
                    <Copy size={15} />
                  </Button>
                  {selected.kind === "tencent_smartsheet" && (
                    <Button variant="danger" onClick={remove}>
                      <Trash2 size={15} /> 删除
                    </Button>
                  )}
                </div>
              }
            />
            <Field label="名称">
              <Input
                value={selected.name}
                onChange={(event) => updateSelected({ name: event.target.value })}
              />
            </Field>

            {selected.kind === "tencent_smartsheet" && draft ? (
              <>
                <div className="settings-tabs pub-tabs" role="tablist" aria-label="腾讯文档配置">
                  {TABS.map((item) => (
                    <button
                      key={item.id}
                      id={`pub-tab-${item.id}`}
                      type="button"
                      role="tab"
                      aria-selected={tab === item.id}
                      aria-controls={`pub-panel-${item.id}`}
                      tabIndex={tab === item.id ? 0 : -1}
                      className={tab === item.id ? "active" : ""}
                      onClick={() => selectTab(item.id)}
                      onKeyDown={onTabKeyDown}
                    >
                      {item.icon}
                      {item.label}
                      {item.id === "mapping" && problems.some((problem) => problem.level === "error") && (
                        <span className="pub-tab-badge">
                          {problems.filter((problem) => problem.level === "error").length}
                        </span>
                      )}
                    </button>
                  ))}
                </div>

                {tab === "connection" && (
                  <div role="tabpanel" id="pub-panel-connection" aria-labelledby="pub-tab-connection">
                    <ConnectionTab
                      draft={draft}
                      upload={upload}
                      onChange={(patch) => applyDraft({ ...draft, ...patch })}
                      onUploadChange={(patch) => {
                        setUpload((current) => ({ ...current, ...patch }));
                        setDirty(true);
                      }}
                    />
                  </div>
                )}
                {tab === "mapping" && (
                  <div role="tabpanel" id="pub-panel-mapping" aria-labelledby="pub-tab-mapping">
                    <MappingTab
                      draft={draft}
                      sources={sources}
                      prompts={prompts}
                      problems={problems}
                      templates={config.smart_sheet?.templates ?? []}
                      onPromptChange={changePrompt}
                      onChange={applyDraft}
                    />
                  </div>
                )}
                {tab === "preview" && preview && (
                  <div role="tabpanel" id="pub-panel-preview" aria-labelledby="pub-tab-preview">
                    <PreviewTab preview={preview} promptName={promptName} />
                  </div>
                )}
              </>
            ) : selected.kind === "excel" ? (
              <div className="onboarding-note">Excel 是默认本地导出，不需要额外配置，也不会产生外部写入。</div>
            ) : selected.kind === "markdown" ? (
              <div className="onboarding-note">Markdown 适合归档、全文检索，或交给其他 AI 继续分析。</div>
            ) : (
              <div className="onboarding-note">飞书文档为预留类型，暂未接入。</div>
            )}
          </section>
        ) : (
          <section className="publish-editor">
            <SectionHeader title="还没有发布目标" description="至少保留 Excel 或 Markdown，也可以新增腾讯文档。" />
          </section>
        )}
      </div>

      <section className="glass-card pub-bind-card">
        <SectionHeader
          title="任务如何绑定"
          description="任务和定时任务各自选择「提示词 + 发布目标」；换目标不会改变提示词，换提示词也不会改变目标。"
        />
        <ul className="coming-list">
          <li>问题雷达：本次运行可选择提示词和发布目标；腾讯文档默认先预览再确认写入。</li>
          <li>定时任务：可在任务里指定默认发布目标；自动同步必须单独开启。</li>
          <li>个人微信 / 高情商回复：只生成草稿，不自动写入外部文档。</li>
        </ul>
      </section>
    </div>
  );
}

import { useMemo, useState } from "react";
import { Copy, Plus, Save, Trash2 } from "lucide-react";
import type { AppConfig, PublishTarget, PublishTargetKind, SmartSheetTemplate } from "../types";
import { useUnsavedChanges } from "../lib/unsaved";
import { Button, Field, Input, SectionHeader } from "../components/ui";

const KIND_LABEL: Record<PublishTargetKind, string> = {
  excel: "Excel 文件",
  markdown: "Markdown 文件",
  tencent_smartsheet: "腾讯文档 Smart Sheet",
  feishu_doc: "飞书文档（预留）",
};

export function targetsFromConfig(config: AppConfig): PublishTarget[] {
  if (config.publish_targets && config.publish_targets.length > 0) {
    return config.publish_targets;
  }
  const targets: PublishTarget[] = [
    { id: "excel", name: "Excel 导出", kind: "excel", config: {} },
    { id: "markdown", name: "Markdown 导出", kind: "markdown", config: {} },
  ];
  for (const template of config.smart_sheet?.templates ?? []) {
    targets.push({
      id: template.id,
      name: template.name || template.id,
      kind: "tencent_smartsheet",
      config: {
        url: template.url,
        webhook_url: template.webhook_url,
        webhook_url_env: template.webhook_url_env,
        batch_size: template.batch_size,
      },
    });
  }
  return targets;
}

function syncSmartSheetMirror(config: AppConfig, targets: PublishTarget[]): AppConfig {
  const smart = config.smart_sheet;
  if (!smart) return config;
  const nextTemplates: SmartSheetTemplate[] = [];
  const seen = new Set<string>();
  for (const target of targets) {
    if (target.kind !== "tencent_smartsheet") continue;
    const existing = smart.templates.find((item) => item.id === target.id);
    const configValue = target.config as Record<string, unknown>;
    nextTemplates.push({
      id: target.id,
      name: target.name || target.id,
      url: String(configValue.url ?? existing?.url ?? ""),
      webhook_url_env: String(configValue.webhook_url_env ?? existing?.webhook_url_env ?? ""),
      webhook_url: String(configValue.webhook_url ?? existing?.webhook_url ?? ""),
      batch_size: Number(configValue.batch_size ?? existing?.batch_size ?? 50),
      schema: existing?.schema ?? {},
      field_mappings: existing?.field_mappings ?? [],
    });
    seen.add(target.id);
  }
  // Keep legacy templates that have not been migrated into targets yet.
  for (const template of smart.templates) {
    if (!seen.has(template.id) && !targets.some((item) => item.kind === "tencent_smartsheet" && item.id === template.id)) {
      nextTemplates.push(template);
    }
  }
  return {
    ...config,
    smart_sheet: {
      ...smart,
      templates: nextTemplates,
      default_template_id: nextTemplates[0]?.id ?? "",
    },
  };
}

function nextId(targets: PublishTarget[], kind: PublishTargetKind): string {
  let index = targets.length + 1;
  let id = `${kind}_${index}`;
  const existing = new Set(targets.map((item) => item.id));
  while (existing.has(id)) {
    index += 1;
    id = `${kind}_${index}`;
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
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  const selected = targets.find((item) => item.id === selectedId) ?? targets[0];
  useUnsavedChanges("publish-targets", dirty);

  const update = (patch: Partial<PublishTarget>) => {
    if (!selected) return;
    setTargets((current) =>
      current.map((item) => (item.id === selected.id ? { ...item, ...patch } : item)),
    );
    setDirty(true);
  };

  const updateConfig = (key: string, value: unknown) => {
    if (!selected) return;
    update({ config: { ...selected.config, [key]: value } });
  };

  const addTarget = (kind: PublishTargetKind) => {
    const id = nextId(targets, kind);
    const target: PublishTarget = {
      id,
      name: KIND_LABEL[kind],
      kind,
      config: kind === "tencent_smartsheet" ? { batch_size: 50 } : {},
    };
    setTargets((current) => [...current, target]);
    setSelectedId(id);
    setDirty(true);
  };

  const duplicate = () => {
    if (!selected) return;
    const id = nextId(targets, selected.kind);
    const copy: PublishTarget = { ...selected, id, name: `${selected.name} 副本` };
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
    setSaving(true);
    try {
      const nextConfig = syncSmartSheetMirror(config, targets);
      await onSave({ ...nextConfig, publish_targets: targets });
      setDirty(false);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page-content">
      <div className="page-title-row">
        <div>
          <span className="eyebrow">策略与发布</span>
          <h1>发布目标</h1>
          <p>决定任务结果写到哪里。提示词负责"怎么整理"，发布目标负责"写到哪里"，两者可以自由组合。</p>
        </div>
        <div className="workbench-card-actions">
          <Button variant="secondary" onClick={() => addTarget("tencent_smartsheet")}>
            <Plus size={15} /> 腾讯文档
          </Button>
          <Button variant="secondary" onClick={() => addTarget("markdown")}>
            <Plus size={15} /> Markdown
          </Button>
          <Button onClick={() => void save()} disabled={!dirty || saving}>
            <Save size={15} /> {saving ? "保存中…" : dirty ? "保存" : "已保存"}
          </Button>
        </div>
      </div>

      <div className="publish-layout">
        <div className="publish-list">
          {targets.map((target) => (
            <button
              key={target.id}
              type="button"
              className={`publish-item ${target.id === selected?.id ? "active" : ""}`}
              onClick={() => setSelectedId(target.id)}
            >
              <strong>{target.name}</strong>
              <span>{KIND_LABEL[target.kind]}</span>
            </button>
          ))}
        </div>

        {selected ? (
          <section className="publish-editor">
            <SectionHeader
              title="目标设置"
              description="同一个提示词可以同时导出 Excel/Markdown，也可以确认后写入腾讯文档。"
              action={
                <div className="workbench-card-actions">
                  <Button variant="ghost" onClick={duplicate}>
                    <Copy size={15} /> 复制
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
              <Input value={selected.name} onChange={(event) => update({ name: event.target.value })} />
            </Field>
            <Field label="类型">
              <Input value={KIND_LABEL[selected.kind]} readOnly />
            </Field>

            {selected.kind === "tencent_smartsheet" && (
              <>
                <Field label="文档 URL" hint="腾讯文档智能表格的分享链接">
                  <Input
                    value={String(selected.config.url ?? "")}
                    onChange={(event) => updateConfig("url", event.target.value)}
                    placeholder="https://docs.qq.com/smartsheet/..."
                  />
                </Field>
                <Field label="Webhook URL" hint="智能表格的接收 Webhook；也可以留空用环境变量">
                  <Input
                    value={String(selected.config.webhook_url ?? "")}
                    onChange={(event) => updateConfig("webhook_url", event.target.value)}
                    placeholder="https://..."
                  />
                </Field>
                <Field label="每批写入条数" hint="默认 50，建议不超过 50">
                  <Input
                    type="number"
                    min={1}
                    max={50}
                    value={Number(selected.config.batch_size ?? 50)}
                    onChange={(event) => updateConfig("batch_size", Number(event.target.value) || 50)}
                  />
                </Field>
                <div className="onboarding-note">
                  字段映射（哪一列写什么）在「设置 → 高级 → 腾讯文档」里编辑。保存后会同步到旧版
                  Smart Sheet 配置，现有任务不需要改。
                </div>
              </>
            )}

            {selected.kind === "excel" && (
              <div className="onboarding-note">Excel 是默认本地导出，不需要额外配置，也不会产生外部写入。</div>
            )}
            {selected.kind === "markdown" && (
              <div className="onboarding-note">Markdown 适合归档、全文检索，或交给其他 AI 继续分析。</div>
            )}
            {selected.kind === "feishu_doc" && (
              <div className="onboarding-note">飞书文档为预留类型，M2 之后再接入。</div>
            )}
          </section>
        ) : (
          <section className="publish-editor">
            <SectionHeader title="还没有发布目标" description="至少保留 Excel 或 Markdown，也可以新增腾讯文档。" />
          </section>
        )}
      </div>

      <section className="glass-card" style={{ marginTop: 16 }}>
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

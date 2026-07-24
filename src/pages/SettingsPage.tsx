import { useEffect, useMemo, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import {
  Bot,
  CheckCircle2,
  Copy,
  Database,
  Eye,
  EyeOff,
  FolderOpen,
  KeyRound,
  Link2,
  LoaderCircle,
  Plus,
  Radar,
  Save,
  Settings2,
  Sparkles,
  Star,
  Table2,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import { loadRunSession, pendingSmartSheetTemplateIds } from "../lib/runSession";
import {
  convertSmartSheetExampleData,
  SUPPORTED_SMART_SHEET_TARGET_TYPES,
} from "../lib/smartSheetSchema";
import type {
  AppConfig,
  EnvironmentDetection,
  ModelConfig,
  SmartSheetFieldMapping,
  SmartSheetFieldSchema,
  SmartSheetTemplate,
} from "../types";
import { Button, Field, Input, SectionHeader } from "../components/ui";

type SettingsTab = "environment" | "models" | "integrations";

const SYSTEM_MAPPING_SOURCES = ["$date", "$images", "$sender", "$message_time", "$issue_key"];
const TARGET_FIELD_TYPES: string[] = [...SUPPORTED_SMART_SHEET_TARGET_TYPES];

const createSmartSheetTemplate = (name = "新腾讯文档模板"): SmartSheetTemplate => ({
  id: `template_${Date.now()}`,
  name,
  url: "",
  webhook_url_env: "",
  webhook_url: "",
  batch_size: 50,
  schema: {},
  field_mappings: [],
});

const cloneTemplate = (template: SmartSheetTemplate): SmartSheetTemplate => ({
  ...template,
  id: `template_${Date.now()}`,
  name: `${template.name}（副本）`,
  schema: Object.fromEntries(Object.entries(template.schema).map(([key, value]) => [
    key,
    { ...value, enum: value.enum ? [...value.enum] : undefined },
  ])),
  field_mappings: template.field_mappings.map((mapping) => ({
    ...mapping,
    default_value: Array.isArray(mapping.default_value) ? [...mapping.default_value] : mapping.default_value,
  })),
});

const schemaDraftsFrom = (config: AppConfig) => Object.fromEntries(
  config.smart_sheet.templates.map((template) => [template.id, JSON.stringify(template.schema, null, 2)]),
);

const mappingDefaultText = (value: unknown) => {
  if (value === undefined || value === null) return "";
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
};

const parseMappingDefault = (text: string, targetType: string): unknown => {
  if (!text.trim()) return "";
  if (targetType === "multiple_select") {
    try {
      const parsed: unknown = JSON.parse(text);
      if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean);
    } catch {
      // Comma/newline input is friendlier for manually entered defaults.
    }
    return text.split(/[,，\n]+/).map((item) => item.trim()).filter(Boolean);
  }
  if (targetType === "number") {
    const number = Number(text);
    return Number.isFinite(number) ? number : text;
  }
  if (targetType === "checkbox" || targetType === "boolean") {
    return ["true", "1", "是", "yes"].includes(text.trim().toLowerCase());
  }
  return text;
};

const isEmptyMappingDefault = (value: unknown) => value === undefined
  || value === null
  || value === ""
  || (Array.isArray(value) && value.length === 0);

function SecretInput({ value, onChange, placeholder }: { value: string; onChange: (value: string) => void; placeholder?: string }) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="secret-input">
      <Input type={visible ? "text" : "password"} value={value} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} />
      <button type="button" onClick={() => setVisible((current) => !current)}>{visible ? <EyeOff size={15} /> : <Eye size={15} />}</button>
    </div>
  );
}
function ModelFields({ title, description, icon, value, onChange, ocr = false }: { title: string; description: string; icon: React.ReactNode; value: ModelConfig; onChange: (value: ModelConfig) => void; ocr?: boolean }) {
  const update = <K extends keyof ModelConfig>(key: K, next: ModelConfig[K]) => onChange({ ...value, [key]: next });
  return (
    <section className="glass-card">
      <SectionHeader title={title} description={description} action={<div className="section-icon">{icon}</div>} />
      <div className="form-grid two-columns">
        {!ocr && <Field label="接口类型"><select className="input" value={value.provider ?? "openai_compatible"} onChange={(event) => update("provider", event.target.value)}><option value="openai_compatible">OpenAI 兼容接口</option><option value="anthropic">Anthropic Messages</option></select></Field>}
        <Field label="Base URL" className={ocr ? "span-two" : undefined}><Input value={value.base_url} placeholder={ocr ? "https://api.anthropic.com" : "https://api.openai.com/v1"} onChange={(event) => update("base_url", event.target.value)} /></Field>
        <Field label="API Key"><SecretInput value={value.api_key} placeholder="仅保存在本机配置中" onChange={(next) => update("api_key", next)} /></Field>
        <Field label="模型"><Input value={value.model} placeholder={ocr ? "claude-sonnet-4-6" : "gpt-5.2"} onChange={(event) => update("model", event.target.value)} /></Field>
        {ocr ? (
          <Field label="并发数" hint="业务电脑建议 2–4"><Input type="number" min={1} max={12} value={value.concurrency ?? 4} onChange={(event) => update("concurrency", Number(event.target.value))} /></Field>
        ) : (
          <Field label="Temperature" hint="问题提炼建议保持 0–0.2"><Input type="number" min={0} max={2} step={0.1} value={value.temperature ?? 0.1} onChange={(event) => update("temperature", Number(event.target.value))} /></Field>
        )}
      </div>
    </section>
  );
}

export function SettingsPage({ config, configPath, onSave }: { config: AppConfig; configPath: string; onSave: (config: AppConfig) => Promise<void> }) {
  const [tab, setTab] = useState<SettingsTab>("environment");
  const [draft, setDraft] = useState<AppConfig>(config);
  const [detection, setDetection] = useState<EnvironmentDetection | null>(null);
  const [detecting, setDetecting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [selectedTemplateId, setSelectedTemplateId] = useState(
    config.smart_sheet.default_template_id || config.smart_sheet.templates[0]?.id || "",
  );
  const [schemaDrafts, setSchemaDrafts] = useState<Record<string, string>>(() => schemaDraftsFrom(config));
  const [schemaErrors, setSchemaErrors] = useState<Record<string, string>>({});
  const [schemaExampleDrafts, setSchemaExampleDrafts] = useState<Record<string, string>>({});
  const [schemaExampleErrors, setSchemaExampleErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    setDraft(config);
    setSelectedTemplateId((current) => config.smart_sheet.templates.some((template) => template.id === current)
      ? current
      : config.smart_sheet.default_template_id || config.smart_sheet.templates[0]?.id || "");
    setSchemaDrafts(schemaDraftsFrom(config));
    setSchemaErrors({});
    setSchemaExampleDrafts({});
    setSchemaExampleErrors({});
  }, [config]);

  const selectedTemplate = draft.smart_sheet.templates.find((template) => template.id === selectedTemplateId)
    ?? draft.smart_sheet.templates[0];
  const mappingSources = useMemo(() => {
    const keys = new Set(SYSTEM_MAPPING_SOURCES);
    draft.prompts.items.forEach((prompt) => prompt.issue_fields?.forEach((field) => keys.add(field.key)));
    return [...keys].sort((left, right) => left.startsWith("$") === right.startsWith("$")
      ? left.localeCompare(right)
      : left.startsWith("$") ? 1 : -1);
  }, [draft.prompts.items]);
  const selectedMappingProblems = useMemo(() => {
    if (!selectedTemplate) return [];
    const problems: string[] = [];
    const targets = new Set<string>();
    selectedTemplate.field_mappings.forEach((mapping) => {
      const target = selectedTemplate.schema[mapping.target_field_id];
      if (!target) problems.push(`目标字段不存在：${mapping.target_field_id || "未填写"}`);
      if (targets.has(mapping.target_field_id)) problems.push(`目标字段重复映射：${mapping.target_field_id}`);
      targets.add(mapping.target_field_id);
      if (target?.type && target.type !== mapping.target_type) {
        problems.push(`${target.title || mapping.target_field_id} 的映射类型应为 ${target.type}`);
      }
      const required = mapping.required || Boolean(target?.title?.startsWith("*"));
      if (!mappingSources.includes(mapping.source_key) && required && isEmptyMappingDefault(mapping.default_value)) {
        problems.push(`必填映射的来源字段不存在：${mapping.source_key || "未填写"}`);
      }
    });
    Object.entries(selectedTemplate.schema).forEach(([fieldId, field]) => {
      if (field.title?.startsWith("*") && !targets.has(fieldId)) {
        problems.push(`缺少必填目标字段映射：${field.title}`);
      }
    });
    if (!Object.keys(selectedTemplate.schema).length) problems.push("尚未定义目标字段 Schema");
    else if (!selectedTemplate.field_mappings.length) problems.push("尚未配置字段映射");
    return [...new Set(problems)];
  }, [mappingSources, selectedTemplate]);

  const updateTemplate = (patch: Partial<SmartSheetTemplate>) => {
    setDraft((previous) => ({
      ...previous,
      smart_sheet: {
        ...previous.smart_sheet,
        templates: previous.smart_sheet.templates.map((template) => template.id === selectedTemplateId
          ? { ...template, ...patch }
          : template),
      },
    }));
  };

  const updateMapping = (index: number, patch: Partial<SmartSheetFieldMapping>) => {
    if (!selectedTemplate) return;
    updateTemplate({
      field_mappings: selectedTemplate.field_mappings.map((mapping, mappingIndex) => mappingIndex === index
        ? { ...mapping, ...patch }
        : mapping),
    });
  };

  const updateSchemaText = (text: string) => {
    if (!selectedTemplate) return;
    const templateId = selectedTemplate.id;
    setSchemaDrafts((previous) => ({ ...previous, [templateId]: text }));
    try {
      const parsed: unknown = JSON.parse(text || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Schema 顶层必须是 JSON 对象");
      }
      if (Object.values(parsed).some((value) => !value || typeof value !== "object" || Array.isArray(value))) {
        throw new Error("每个目标字段都必须是对象");
      }
      updateTemplate({ schema: parsed as Record<string, SmartSheetFieldSchema> });
      setSchemaErrors((previous) => ({ ...previous, [templateId]: "" }));
    } catch (error) {
      setSchemaErrors((previous) => ({
        ...previous,
        [templateId]: error instanceof Error ? error.message : "Schema JSON 格式无效",
      }));
    }
  };

  const importSchemaExample = () => {
    if (!selectedTemplate) return;
    const templateId = selectedTemplate.id;
    try {
      const conversion = convertSmartSheetExampleData(schemaExampleDrafts[templateId] ?? "");
      updateSchemaText(JSON.stringify(conversion.schema, null, 2));
      setSchemaExampleErrors((previous) => ({ ...previous, [templateId]: "" }));
      const descriptions: string[] = [];
      if (conversion.unsupportedTypes.length) {
        descriptions.push(`暂不支持映射的类型：${conversion.unsupportedTypes.join("、")}`);
      }
      if (selectedTemplate.field_mappings.length) {
        descriptions.push(`已保留 ${selectedTemplate.field_mappings.length} 条现有映射，请重新核对目标字段和类型。`);
      }
      toast.success(`已从示例数据提取 ${Object.keys(conversion.schema).length} 个字段`, {
        description: descriptions.join(" ") || "请检查字段后保存设置。",
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "无法转换示例数据";
      setSchemaExampleErrors((previous) => ({ ...previous, [templateId]: message }));
      toast.warning("示例数据转换失败", { description: message });
    }
  };

  const addTemplate = () => {
    const template = createSmartSheetTemplate();
    while (draft.smart_sheet.templates.some((item) => item.id === template.id)) template.id += "_new";
    setDraft((previous) => ({
      ...previous,
      smart_sheet: { ...previous.smart_sheet, templates: [...previous.smart_sheet.templates, template] },
    }));
    setSchemaDrafts((previous) => ({ ...previous, [template.id]: "{}" }));
    setSchemaExampleDrafts((previous) => ({ ...previous, [template.id]: "" }));
    setSelectedTemplateId(template.id);
  };

  const duplicateTemplate = () => {
    if (!selectedTemplate) return;
    const template = cloneTemplate(selectedTemplate);
    while (draft.smart_sheet.templates.some((item) => item.id === template.id)) template.id += "_copy";
    setDraft((previous) => ({
      ...previous,
      smart_sheet: { ...previous.smart_sheet, templates: [...previous.smart_sheet.templates, template] },
    }));
    setSchemaDrafts((previous) => ({ ...previous, [template.id]: JSON.stringify(template.schema, null, 2) }));
    setSchemaExampleDrafts((previous) => ({ ...previous, [template.id]: "" }));
    setSelectedTemplateId(template.id);
  };

  const removeTemplate = () => {
    if (!selectedTemplate || draft.smart_sheet.templates.length <= 1) return;
    const referencedSchedules = draft.schedules?.filter(
      (schedule) => schedule.smartSheetTemplateId === selectedTemplate.id,
    ) ?? [];
    if (referencedSchedules.length) {
      toast.warning("该腾讯文档模板正在被定时任务使用", {
        description: `请先修改定时任务：${referencedSchedules.map((schedule) => schedule.name).join("、")}`,
      });
      return;
    }
    try {
      const restored = loadRunSession(window.localStorage);
      if (pendingSmartSheetTemplateIds(restored?.result ?? null).includes(selectedTemplate.id)) {
        toast.warning("该腾讯文档模板仍有手动任务待确认", {
          description: "请回到“开始处理”，完成同步或在确认框中选择“放弃待同步”，再删除模板。",
        });
        return;
      }
    } catch {
      // Unavailable browser storage must not prevent normal settings maintenance.
    }
    if (!window.confirm(`确定删除腾讯文档模板“${selectedTemplate.name}”吗？`)) return;
    const remaining = draft.smart_sheet.templates.filter((template) => template.id !== selectedTemplate.id);
    const nextDefault = draft.smart_sheet.default_template_id === selectedTemplate.id
      ? remaining[0].id
      : draft.smart_sheet.default_template_id;
    setDraft((previous) => ({
      ...previous,
      prompts: {
        ...previous.prompts,
        items: previous.prompts.items.map((prompt) => prompt.default_smart_sheet_template_id === selectedTemplate.id
          ? { ...prompt, default_smart_sheet_template_id: "" }
          : prompt),
      },
      smart_sheet: { ...previous.smart_sheet, default_template_id: nextDefault, templates: remaining },
    }));
    setSchemaDrafts((previous) => {
      const next = { ...previous };
      delete next[selectedTemplate.id];
      return next;
    });
    setSchemaErrors((previous) => {
      const next = { ...previous };
      delete next[selectedTemplate.id];
      return next;
    });
    setSchemaExampleDrafts((previous) => {
      const next = { ...previous };
      delete next[selectedTemplate.id];
      return next;
    });
    setSchemaExampleErrors((previous) => {
      const next = { ...previous };
      delete next[selectedTemplate.id];
      return next;
    });
    setSelectedTemplateId(remaining[0].id);
  };

  const addMapping = () => {
    if (!selectedTemplate) return;
    const usedTargets = new Set(selectedTemplate.field_mappings.map((mapping) => mapping.target_field_id));
    const target = Object.keys(selectedTemplate.schema).find((fieldId) => !usedTargets.has(fieldId));
    if (!target) {
      toast.warning("没有可映射的目标字段", { description: "请先在目标字段 Schema 中增加字段，或删除重复映射。" });
      return;
    }
    const targetSchema = selectedTemplate.schema[target] ?? {};
    const source = mappingSources.find((key) => !selectedTemplate.field_mappings.some((mapping) => mapping.source_key === key))
      ?? mappingSources[0]
      ?? "problem_description";
    updateTemplate({
      field_mappings: [...selectedTemplate.field_mappings, {
        source_key: source,
        target_field_id: target,
        target_type: targetSchema.type ?? "text",
        required: Boolean(targetSchema.title?.startsWith("*")),
        default_value: "",
      }],
    });
  };

  const save = async () => {
    const invalidSchema = Object.entries(schemaErrors).find(([, message]) => Boolean(message));
    if (invalidSchema) {
      setTab("integrations");
      setSelectedTemplateId(invalidSchema[0]);
      toast.warning("目标字段 Schema 不是有效 JSON", { description: invalidSchema[1] });
      return;
    }
    for (const template of draft.smart_sheet.templates) {
      if (!template.name.trim()) {
        toast.warning("腾讯文档模板名称不能为空");
        return;
      }
      const targets = new Set<string>();
      for (const mapping of template.field_mappings) {
        if (!mapping.source_key.trim() || !mapping.target_field_id.trim()) {
          toast.warning(`“${template.name}”存在未完成的字段映射`);
          return;
        }
        if (targets.has(mapping.target_field_id)) {
          toast.warning(`“${template.name}”中的目标字段 ${mapping.target_field_id} 被重复映射`);
          return;
        }
        targets.add(mapping.target_field_id);
      }
    }
    setSaving(true);
    try {
      await onSave(draft);
      toast.success("设置已保存");
    } catch (error) {
      toast.error("设置保存失败", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
    } finally {
      setSaving(false);
    }
  };

  const detect = async () => {
    setDetecting(true);
    try {
      const result = await bridge.detectEnvironment();
      setDetection(result);
      if (result.dataDirectories.length) setDraft((previous) => ({ ...previous, wxwork_db_dir: result.dataDirectories[0] }));
      toast.success(result.dataDirectories.length ? "已找到企业微信数据目录" : "检测完成，未自动找到数据目录");
    } catch (error) {
      toast.error("自动检测失败", {
        description: toUserErrorMessage(error, "请确认企业微信已安装后重试。"),
      });
    } finally {
      setDetecting(false);
    }
  };

  const pickDirectory = async (key: "wxwork_db_dir" | "default_workspace") => {
    try {
      const selected = await open({ directory: true, multiple: false, title: key === "wxwork_db_dir" ? "选择企业微信 Data 目录" : "选择导出目录" });
      if (selected) setDraft((previous) => ({ ...previous, [key]: selected }));
    } catch (error) {
      toast.error("无法打开目录选择器", {
        description: toUserErrorMessage(error, "请稍后重试，或手动填写目录。"),
      });
    }
  };

  const launchKeyExtraction = async () => {
    try {
      await bridge.launchKeyExtraction();
    } catch (error) {
      toast.error("无法启动密钥提取", {
        description: toUserErrorMessage(error, "请确认企业微信正在运行后重试。"),
      });
    }
  };

  return (
    <div className="page-content settings-page">
      <div className="page-title-row">
        <div><div className="eyebrow"><Settings2 size={13} />Preferences</div><h1>设置</h1><p>配置数据源、模型和腾讯文档集成。敏感信息只保存在本机。</p></div>
        <Button onClick={() => void save()} disabled={saving}><Save size={16} />{saving ? "保存中" : "保存设置"}</Button>
      </div>
      <div className="settings-tabs">
        <button className={tab === "environment" ? "active" : ""} onClick={() => setTab("environment")}><Database size={16} />企业微信与目录</button>
        <button className={tab === "models" ? "active" : ""} onClick={() => setTab("models")}><Bot size={16} />模型与 OCR</button>
        <button className={tab === "integrations" ? "active" : ""} onClick={() => setTab("integrations")}><Table2 size={16} />腾讯文档</button>
      </div>

      {tab === "environment" && <div className="settings-stack">
        <section className="glass-card detection-card">
          <SectionHeader title="自动检测" description="扫描企业微信安装状态和最近使用的数据目录。" action={<Button variant="secondary" onClick={() => void detect()} disabled={detecting}>{detecting ? <LoaderCircle size={15} className="spin" /> : <Radar size={15} />}{detecting ? "检测中" : "立即检测"}</Button>} />
          {detection ? <div className="detection-result">
            <div><span className={detection.running ? "result-icon success" : "result-icon"}><CheckCircle2 size={16} /></span><span><strong>企业微信{detection.running ? "正在运行" : "当前未运行"}</strong><small>{detection.executablePaths[0] || "未检测到安装路径，不影响手动配置"}</small></span></div>
            <div><span className={detection.dataDirectories.length ? "result-icon success" : "result-icon"}><Database size={16} /></span><span><strong>{detection.dataDirectories.length ? `找到 ${detection.dataDirectories.length} 个数据目录` : "未找到数据目录"}</strong><small>{detection.dataDirectories[0] || "可在下方手动选择"}</small></span></div>
          </div> : <div className="detection-empty"><Radar size={24} /><span>点击“立即检测”，推荐路径会自动填入下方设置。</span></div>}
        </section>

        <section className="glass-card">
          <SectionHeader title="数据与导出目录" description={`配置文件：${configPath}`} />
          <div className="form-grid">
            <Field label="企业微信 Data 目录" hint="通常位于 Documents\\WXWork\\账号 ID\\Data">
              <div className="input-action"><Input value={draft.wxwork_db_dir} onChange={(event) => setDraft({ ...draft, wxwork_db_dir: event.target.value })} /><Button variant="secondary" onClick={() => void pickDirectory("wxwork_db_dir")}><FolderOpen size={15} />选择</Button></div>
            </Field>
            <Field label="结果导出目录" hint="按群和日期自动建立隔离目录">
              <div className="input-action"><Input value={draft.default_workspace} onChange={(event) => setDraft({ ...draft, default_workspace: event.target.value })} /><Button variant="secondary" onClick={() => void pickDirectory("default_workspace")}><FolderOpen size={15} />选择</Button></div>
            </Field>
            <Field label="数据库密钥文件" hint="首次使用需要在企业微信运行时提取"><div className="input-action"><Input value={draft.wxwork_keys_file} onChange={(event) => setDraft({ ...draft, wxwork_keys_file: event.target.value })} /><Button variant="secondary" onClick={() => void launchKeyExtraction()}><KeyRound size={15} />提取密钥</Button></div></Field>
          </div>
        </section>
      </div>}

      {tab === "models" && <div className="settings-stack">
        <ModelFields title="大模型分析" description="用于理解上下文、合并同类问题并生成结构化问题清单。" icon={<Sparkles size={19} />} value={draft.llm} onChange={(llm) => setDraft({ ...draft, llm })} />
        <ModelFields title="截图 OCR" description="使用支持图片输入的模型识别聊天截图，独立于文本分析模型。" icon={<Bot size={19} />} value={draft.ocr} ocr onChange={(ocr) => setDraft({ ...draft, ocr })} />
        <div className="privacy-note"><KeyRound size={17} /><div><strong>凭据安全</strong><p>API Key 会写入当前 Windows 用户目录下的本地配置文件，不会提交到 GitHub，也不会上传到本项目的任何服务。</p></div></div>
      </div>}

      {tab === "integrations" && <div className="settings-stack">
        <section className="glass-card smart-template-library">
          <SectionHeader
            title="腾讯文档模板库"
            description="先定义目标表格及字段映射；运行时按提示词自动选择，也可以临时更换。"
            action={<Button variant="secondary" onClick={addTemplate}><Plus size={15} />新建模板</Button>}
          />
          <div className="smart-template-layout">
            <aside className="smart-template-list">
              {draft.smart_sheet.templates.map((template) => (
                <button
                  type="button"
                  key={template.id}
                  className={template.id === selectedTemplate?.id ? "smart-template-item active" : "smart-template-item"}
                  onClick={() => setSelectedTemplateId(template.id)}
                >
                  <span className="prompt-list-icon"><Table2 size={16} /></span>
                  <span><strong>{template.name}</strong><small>{template.field_mappings.length} 条映射 · {Object.keys(template.schema).length} 个目标字段</small></span>
                  {template.id === draft.smart_sheet.default_template_id && <span className="default-chip"><Star size={10} fill="currentColor" />默认</span>}
                </button>
              ))}
            </aside>

            {selectedTemplate && <div className="smart-template-editor">
              <div className="smart-template-editor-heading">
                <div><span className="section-kicker">Template · {selectedTemplate.id}</span><h3>{selectedTemplate.name}</h3></div>
                <div className="inline-actions">
                  <Button variant="ghost" title="复制模板" onClick={duplicateTemplate}><Copy size={15} /></Button>
                  <Button variant="ghost" title="删除模板" disabled={draft.smart_sheet.templates.length <= 1} onClick={removeTemplate}><Trash2 size={15} /></Button>
                </div>
              </div>
              <div className="form-grid two-columns">
                <Field label="模板名称"><Input value={selectedTemplate.name} onChange={(event) => updateTemplate({ name: event.target.value })} /></Field>
                <Field label="单批写入数量" hint="范围 1–50"><Input type="number" min={1} max={50} value={selectedTemplate.batch_size} onChange={(event) => updateTemplate({ batch_size: Math.max(1, Math.min(50, Number(event.target.value) || 1)) })} /></Field>
                <Field label="Smart Sheet 地址" hint="用于确认弹窗展示目标文档"><Input value={selectedTemplate.url} placeholder="https://docs.qq.com/sheet/..." onChange={(event) => updateTemplate({ url: event.target.value })} /></Field>
                <Field label="Webhook 环境变量" hint="可留空并直接填写下方 URL"><Input value={selectedTemplate.webhook_url_env} placeholder="WECOM_SMARTSHEET_WEBHOOK_URL" onChange={(event) => updateTemplate({ webhook_url_env: event.target.value })} /></Field>
                <Field label="写入 Webhook URL" className="span-two" hint="接收 records 数组并写入该模板对应的 Smart Sheet"><SecretInput value={selectedTemplate.webhook_url} placeholder="https://..." onChange={(webhook_url) => updateTemplate({ webhook_url })} /></Field>
              </div>

              <button
                type="button"
                className={selectedTemplate.id === draft.smart_sheet.default_template_id ? "default-selector selected" : "default-selector"}
                onClick={() => setDraft((previous) => ({
                  ...previous,
                  smart_sheet: { ...previous.smart_sheet, default_template_id: selectedTemplate.id },
                }))}
              >
                <span className="radio-mark">{selectedTemplate.id === draft.smart_sheet.default_template_id && <CheckCircle2 size={13} />}</span>
                <span><strong>设为全局默认模板</strong><small>没有单独绑定腾讯模板的提示词会使用它</small></span>
              </button>

              <div className="template-config-section">
                <div className="template-config-heading"><div><h3>目标字段 Schema</h3><p>粘贴腾讯文档模板的字段 ID、名称、类型和枚举选项。</p></div><span>{Object.keys(selectedTemplate.schema).length} 个字段</span></div>
                <details className="schema-example-importer">
                  <summary><Sparkles size={14} /><span><strong>从腾讯示例数据转换</strong><small>粘贴包含 schema 和 add_records 的完整示例 JSON</small></span></summary>
                  <div className="schema-example-importer-body">
                    <textarea
                      aria-label="腾讯文档示例数据"
                      className={schemaExampleErrors[selectedTemplate.id] ? "textarea schema-example-textarea input-invalid" : "textarea schema-example-textarea"}
                      spellCheck={false}
                      value={schemaExampleDrafts[selectedTemplate.id] ?? ""}
                      placeholder={'{\n  "schema": {\n    "field_id": { "title": "问题描述", "type": "text" }\n  },\n  "add_records": []\n}'}
                      onChange={(event) => {
                        const text = event.target.value;
                        setSchemaExampleDrafts((previous) => ({ ...previous, [selectedTemplate.id]: text }));
                        if (schemaExampleErrors[selectedTemplate.id]) {
                          setSchemaExampleErrors((previous) => ({ ...previous, [selectedTemplate.id]: "" }));
                        }
                      }}
                    />
                    {schemaExampleErrors[selectedTemplate.id] && <p className="schema-error">转换失败：{schemaExampleErrors[selectedTemplate.id]}</p>}
                    <div className="schema-example-actions">
                      <p>转换会覆盖下方 Schema，但保留现有字段映射；保存前请重新核对映射。</p>
                      <Button type="button" variant="secondary" onClick={importSchemaExample}><Sparkles size={14} />提取并填入 Schema</Button>
                    </div>
                  </div>
                </details>
                <textarea
                  className={schemaErrors[selectedTemplate.id] ? "textarea schema-textarea input-invalid" : "textarea schema-textarea"}
                  spellCheck={false}
                  value={schemaDrafts[selectedTemplate.id] ?? JSON.stringify(selectedTemplate.schema, null, 2)}
                  placeholder={'{\n  "field_id": { "title": "*问题描述", "type": "text" }\n}'}
                  onChange={(event) => updateSchemaText(event.target.value)}
                />
                {schemaErrors[selectedTemplate.id] && <p className="schema-error">JSON 无效：{schemaErrors[selectedTemplate.id]}</p>}
              </div>

              <div className="template-config-section">
                <div className="template-config-heading">
                  <div><h3>字段映射</h3><p>把问题清单或系统字段映射到腾讯文档列；默认值用于源字段为空时回填。</p></div>
                  <Button variant="secondary" onClick={addMapping}><Plus size={14} />添加映射</Button>
                </div>
                <datalist id={`mapping-sources-${selectedTemplate.id}`}>
                  {mappingSources.map((source) => <option key={source} value={source} />)}
                </datalist>
                {selectedMappingProblems.length > 0 && <div className="mapping-problems">
                  {selectedMappingProblems.map((problem) => <span key={problem}>{problem}</span>)}
                </div>}
                {selectedTemplate.field_mappings.length ? <div className="mapping-list">
                  {selectedTemplate.field_mappings.map((mapping, index) => (
                    <div className="mapping-row" key={`${mapping.target_field_id}-${index}`}>
                      <Field label="来源字段">
                        <Input list={`mapping-sources-${selectedTemplate.id}`} value={mapping.source_key} onChange={(event) => updateMapping(index, { source_key: event.target.value })} />
                      </Field>
                      <Field label="目标字段">
                        <select
                          className="input"
                          value={mapping.target_field_id}
                          onChange={(event) => {
                            const target_field_id = event.target.value;
                            const targetSchema = selectedTemplate.schema[target_field_id] ?? {};
                            updateMapping(index, {
                              target_field_id,
                              target_type: targetSchema.type ?? mapping.target_type,
                              required: Boolean(targetSchema.title?.startsWith("*")),
                            });
                          }}
                        >
                          {!selectedTemplate.schema[mapping.target_field_id] && <option value={mapping.target_field_id}>{mapping.target_field_id || "请选择目标字段"}</option>}
                          {Object.entries(selectedTemplate.schema).map(([fieldId, field]) => <option key={fieldId} value={fieldId}>{field.title || fieldId} · {fieldId}</option>)}
                        </select>
                      </Field>
                      <Field label="目标类型">
                        <select className="input" value={mapping.target_type} onChange={(event) => updateMapping(index, { target_type: event.target.value })}>
                          {!TARGET_FIELD_TYPES.includes(mapping.target_type) && <option value={mapping.target_type}>{mapping.target_type}</option>}
                          {TARGET_FIELD_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
                        </select>
                      </Field>
                      <Field label="默认值"><Input value={mappingDefaultText(mapping.default_value)} placeholder="可留空" onChange={(event) => updateMapping(index, { default_value: parseMappingDefault(event.target.value, mapping.target_type) })} /></Field>
                      <label className="mapping-required"><span>必填</span><input
                        type="checkbox"
                        checked={mapping.required || Boolean(selectedTemplate.schema[mapping.target_field_id]?.title?.startsWith("*"))}
                        disabled={Boolean(selectedTemplate.schema[mapping.target_field_id]?.title?.startsWith("*"))}
                        title={selectedTemplate.schema[mapping.target_field_id]?.title?.startsWith("*") ? "目标 Schema 已标记为必填" : undefined}
                        onChange={(event) => updateMapping(index, { required: event.target.checked })}
                      /></label>
                      <Button variant="ghost" title="删除映射" onClick={() => updateTemplate({ field_mappings: selectedTemplate.field_mappings.filter((_, mappingIndex) => mappingIndex !== index) })}><Trash2 size={14} /></Button>
                    </div>
                  ))}
                </div> : <div className="mapping-empty"><Table2 size={21} /><span>还没有字段映射。先填写目标字段 Schema，再添加映射。</span></div>}
              </div>
            </div>}
          </div>
        </section>
        <section className="glass-card">
          <SectionHeader title="腾讯文档图片写入" description="按照 Smart Sheet 官方格式直接写入图片内容。" action={<div className="section-icon"><Link2 size={19} /></div>} />
          <div className="privacy-note"><CheckCircle2 size={17} /><div><strong>无需额外凭据</strong><p>图片会在本机转换为 Base64 后随 Webhook 写入，不再依赖 Corp ID、Corp Secret 或外部图片 URL。</p></div></div>
        </section>
        <div className="privacy-note"><Table2 size={17} /><div><strong>需要腾讯侧写入接口</strong><p>Smart Sheet 目前通过配置的 Webhook 写入；本项目会按字段 Schema 生成 records。若暂不配置，Excel 和 Markdown 导出完全不受影响。</p></div></div>
      </div>}
    </div>
  );
}

import { useEffect, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import {
  Bot,
  CheckCircle2,
  Database,
  Eye,
  EyeOff,
  FolderOpen,
  KeyRound,
  Link2,
  LoaderCircle,
  Radar,
  Save,
  Settings2,
  Sparkles,
  Table2,
} from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import type { AppConfig, EnvironmentDetection, ModelConfig } from "../types";
import { Button, Field, Input, SectionHeader } from "../components/ui";

type SettingsTab = "environment" | "models" | "integrations";

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
  useEffect(() => setDraft(config), [config]);

  const save = async () => {
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
        <section className="glass-card">
          <SectionHeader title="腾讯文档 Smart Sheet" description="可选集成。任务完成后先显示待写入数量，只有再次确认才会同步。" action={<div className="section-icon"><Table2 size={19} /></div>} />
          <div className="form-grid">
            <Field label="Smart Sheet 地址" hint="用于业务人员快速打开目标表格"><Input value={draft.smart_sheet.url} placeholder="https://docs.qq.com/sheet/..." onChange={(event) => setDraft({ ...draft, smart_sheet: { ...draft.smart_sheet, url: event.target.value } })} /></Field>
            <Field label="写入 Webhook URL" hint="接收 records 数组并写入目标 Smart Sheet 的腾讯侧接口"><SecretInput value={draft.smart_sheet.webhook_url} placeholder="https://..." onChange={(webhook_url) => setDraft({ ...draft, smart_sheet: { ...draft.smart_sheet, webhook_url } })} /></Field>
          </div>
        </section>
        <section className="glass-card">
          <SectionHeader title="企业微信图片上传" description="问题截图同步到 Smart Sheet 时，需要企业 ID 和应用 Secret。" action={<div className="section-icon"><Link2 size={19} /></div>} />
          <div className="form-grid two-columns">
            <Field label="Corp ID"><Input value={draft.smart_sheet.upload.corpid} placeholder="ww..." onChange={(event) => setDraft({ ...draft, smart_sheet: { ...draft.smart_sheet, upload: { ...draft.smart_sheet.upload, corpid: event.target.value } } })} /></Field>
            <Field label="Corp Secret"><SecretInput value={draft.smart_sheet.upload.corpsecret} placeholder="仅在同步图片时使用" onChange={(corpsecret) => setDraft({ ...draft, smart_sheet: { ...draft.smart_sheet, upload: { ...draft.smart_sheet.upload, corpsecret } } })} /></Field>
          </div>
        </section>
        <div className="privacy-note"><Table2 size={17} /><div><strong>需要腾讯侧写入接口</strong><p>Smart Sheet 目前通过配置的 Webhook 写入；本项目会按字段 Schema 生成 records。若暂不配置，Excel 和 Markdown 导出完全不受影响。</p></div></div>
      </div>}
    </div>
  );
}

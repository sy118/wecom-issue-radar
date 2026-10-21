import { useEffect, useState } from "react";
import { Download, FolderOpen, KeyRound, LoaderCircle, Radar, RefreshCw, Save, Settings2, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import { getDesktop } from "../lib/desktop";
import { toUserErrorMessage } from "../lib/errors";
import { applyZoom, getSavedZoom, ZOOM_LEVELS } from "../lib/zoom";
import { useUnsavedChanges } from "../lib/unsaved";
import type { AppConfig, EnvironmentDetection } from "../types";
import { Button, Field, Input } from "../components/ui";
import { SettingsPage as SettingsAdvancedPage } from "./SettingsPage";

export function SettingsHome({
  config,
  configPath,
  version,
  onSave,
  onImport,
  onRestartOnboarding,
}: {
  config: AppConfig;
  configPath: string;
  version: string;
  onSave: (config: AppConfig) => Promise<void>;
  onImport: (path: string) => Promise<void>;
  onRestartOnboarding: () => void;
}) {
  const [mode, setMode] = useState<"common" | "advanced">("common");
  const [draft, setDraft] = useState<AppConfig>(config);
  const [saving, setSaving] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [detection, setDetection] = useState<EnvironmentDetection | null>(null);
  const [zoom, setZoom] = useState(getSavedZoom());
  const [updating, setUpdating] = useState(false);

  useEffect(() => {
    setDraft(config);
  }, [config]);

  const dirty = JSON.stringify(draft) !== JSON.stringify(config);
  useUnsavedChanges("settings-common", dirty && mode === "common");

  const save = async () => {
    setSaving(true);
    try {
      await onSave(draft);
      toast.success("设置已保存");
    } catch (error) {
      toast.error("保存失败", { description: toUserErrorMessage(error, "请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  const detect = async () => {
    setDetecting(true);
    try {
      const result = await bridge.detectEnvironment();
      setDetection(result);
      if (result.dataDirectories[0]) {
        setDraft((current) => ({ ...current, wxwork_db_dir: result.dataDirectories[0] }));
      }
      toast.success(result.dataDirectories.length > 0 ? "已找到企业微信目录" : "未找到目录，请手动选择");
    } catch (error) {
      toast.error("检测失败", { description: toUserErrorMessage(error, "请稍后重试。") });
    } finally {
      setDetecting(false);
    }
  };

  const chooseDirectory = async (key: "wxwork_db_dir" | "default_workspace") => {
    try {
      const selected = await getDesktop().invoke<string | null>("dialog_open", {
        directory: true,
        multiple: false,
        title: key === "wxwork_db_dir" ? "选择企业微信 Data 目录" : "选择导出目录",
      });
      if (selected) setDraft((current) => ({ ...current, [key]: selected }));
    } catch (error) {
      toast.error("无法打开目录选择器", { description: toUserErrorMessage(error, "请手动填写目录。") });
    }
  };

  const launchKeys = async () => {
    try {
      await bridge.launchKeyExtraction();
      toast.success("已打开密钥提取窗口");
    } catch (error) {
      toast.error("无法打开密钥提取", {
        description: toUserErrorMessage(error, "请确认企业微信已登录并保持运行。"),
      });
    }
  };

  const changeZoom = async (factor: number) => {
    setZoom(factor);
    await applyZoom(factor);
  };

  const checkUpdates = async () => {
    setUpdating(true);
    try {
      await getDesktop().invoke("updater_check");
      toast.success("已检查更新");
    } catch (error) {
      toast.error("检查更新失败", { description: toUserErrorMessage(error, "请稍后重试。") });
    } finally {
      setUpdating(false);
    }
  };

  return (
    <div className="page-content">
      <div className="page-title-row">
        <div>
          <span className="eyebrow">设置</span>
          <h1>常用设置</h1>
          <p>日常只需要配置这几项。更细的选项在「高级」里，随时可以再改。</p>
        </div>
        <div className="settings-mode-tabs">
          <button type="button" className={mode === "common" ? "active" : ""} onClick={() => setMode("common")}>
            常用
          </button>
          <button type="button" className={mode === "advanced" ? "active" : ""} onClick={() => setMode("advanced")}>
            高级
          </button>
        </div>
      </div>

      {mode === "advanced" ? (
        <div className="settings-advanced-embed">
          <SettingsAdvancedPage
            config={config}
            configPath={configPath}
            onSave={onSave}
            onImport={onImport}
          />
        </div>
      ) : (
        <div className="settings-common">
          <section className="settings-common-card">
            <h2>企业微信数据</h2>
            <p>用于读取本机群聊记录。点「自动检测」最省事；检测不到再手动选择 Data 目录。</p>
            <Field label="Data 目录">
              <div className="input-action">
                <Input
                  value={draft.wxwork_db_dir}
                  onChange={(event) => setDraft({ ...draft, wxwork_db_dir: event.target.value })}
                  placeholder="C:\\Users\\你\\Documents\\WXWork\\...\\Data"
                />
                <Button variant="secondary" onClick={() => void chooseDirectory("wxwork_db_dir")}>
                  <FolderOpen size={15} /> 选择
                </Button>
              </div>
            </Field>
            <div className="workbench-card-actions">
              <Button variant="secondary" onClick={() => void detect()} disabled={detecting}>
                {detecting ? <LoaderCircle className="spin" size={15} /> : <Radar size={15} />}
                {detecting ? "检测中…" : "自动检测"}
              </Button>
              <Button variant="secondary" onClick={() => void launchKeys()}>
                <KeyRound size={15} /> 提取数据库密钥
              </Button>
            </div>
            {detection && (
              <div className="onboarding-diag">
                <strong>{detection.running ? "企业微信正在运行" : "企业微信未运行"}</strong>
                {detection.dataDirectories.map((directory) => (
                  <code key={directory}>{directory}</code>
                ))}
              </div>
            )}
          </section>

          <section className="settings-common-card">
            <h2>大模型</h2>
            <p>用于整理问题、群监听拟稿。只导出 Excel 可以先不配。</p>
            <Field label="Base URL">
              <Input
                value={draft.llm.base_url}
                onChange={(event) => setDraft({ ...draft, llm: { ...draft.llm, base_url: event.target.value } })}
                placeholder="https://api.example.com/v1"
              />
            </Field>
            <Field label="API Key">
              <Input
                type="password"
                value={draft.llm.api_key}
                onChange={(event) => setDraft({ ...draft, llm: { ...draft.llm, api_key: event.target.value } })}
                placeholder="sk-..."
              />
            </Field>
            <Field label="模型名称">
              <Input
                value={draft.llm.model}
                onChange={(event) => setDraft({ ...draft, llm: { ...draft.llm, model: event.target.value } })}
                placeholder="deepseek-chat"
              />
            </Field>
          </section>

          <section className="settings-common-card">
            <h2>导出位置</h2>
            <p>Excel、Markdown 和图片缓存会写到这里。</p>
            <Field label="导出目录">
              <div className="input-action">
                <Input
                  value={draft.default_workspace}
                  onChange={(event) => setDraft({ ...draft, default_workspace: event.target.value })}
                />
                <Button variant="secondary" onClick={() => void chooseDirectory("default_workspace")}>
                  <FolderOpen size={15} /> 选择
                </Button>
              </div>
            </Field>
          </section>

          <section className="settings-common-card">
            <h2>界面大小</h2>
            <p>觉得字小可以调大，整个界面会一起缩放。</p>
            <div className="settings-mode-tabs">
              {ZOOM_LEVELS.map((level) => (
                <button
                  key={level}
                  type="button"
                  className={Math.abs(zoom - level) < 0.001 ? "active" : ""}
                  onClick={() => void changeZoom(level)}
                >
                  {Math.round(level * 100)}%
                </button>
              ))}
            </div>
          </section>

          <section className="settings-common-card">
            <h2>个人微信</h2>
            <p>
              <Sparkles size={15} /> 个人微信会话读取、草稿与「填入但不发送」将在 M2 融合版本提供。
            </p>
          </section>

          <section className="settings-common-card">
            <h2>新手引导</h2>
            <p>重新走一遍三条路径的介绍和首次导出/监听引导。只重置引导进度，不改动已保存的配置。</p>
            <div className="workbench-card-actions">
              <Button variant="secondary" onClick={onRestartOnboarding}>
                <RefreshCw size={15} /> 重新观看新手引导
              </Button>
            </div>
          </section>

          <section className="settings-common-card">
            <h2>更新与关于</h2>
            <p>当前版本 v{version}。更新包由 GitHub Actions 构建，安装前会先停止后台任务。</p>
            <div className="workbench-card-actions">
              <Button variant="secondary" onClick={() => void checkUpdates()} disabled={updating}>
                {updating ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}
                {updating ? "检查中…" : "检查更新"}
              </Button>
              <Button variant="ghost" onClick={() => void applyZoom(1)}>
                <Settings2 size={15} /> 恢复 100%
              </Button>
            </div>
          </section>

          <div className="workbench-card-actions" style={{ justifyContent: "flex-end" }}>
            <Button onClick={() => void save()} disabled={!dirty || saving}>
              {saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
              {saving ? "保存中…" : dirty ? "保存设置" : "已保存"}
            </Button>
          </div>
          <p className="field-hint">配置文件：{configPath}</p>
          <div className="workbench-card-actions">
            <Button variant="ghost" onClick={() => setMode("advanced")}>
              <Download size={15} /> 打开高级设置（备份、MCP、腾讯文档字段映射等）
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Personal WeChat configuration center (``PageId = "wechat-config"``).
 *
 * One full page instead of stacked drawers: WeChat data discovery, project
 * defaults, editable reply styles, read-only model/MCP overview, export and
 * advanced data tools all live here. The workbench keeps only conversations
 * and drafts.
 */

import { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  Bot,
  CheckCircle2,
  Database,
  Download,
  FolderOpen,
  Image as ImageIcon,
  Import,
  KeyRound,
  LoaderCircle,
  MessageCircle,
  Network,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  Sparkles,
  Trash2,
  Wand2,
} from "lucide-react";
import clsx from "clsx";

import { Field, Input, Switch } from "../../components/ui";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ProjectEditorDrawer } from "../../components/wechat/ProjectEditorDrawer";
import { useWechatConfig } from "./useWechatConfig";
import * as api from "./api";
import {
  DEFAULT_REPLY_STYLES,
  TONE_PRESETS,
  type TriggerMode,
  type WechatProject,
  type WechatRuntimeStatus,
} from "./types";
import "../../wechat.css";
import "./wechat-config.css";

export interface WechatConfigPageProps {
  onNavigate?: (page: string) => void;
}

const TRIGGER_MODE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "跟随会话类型（单聊自动 / 群聊手动）" },
  { value: "auto", label: "自动：有新消息就准备草稿" },
  { value: "manual", label: "手动：仅在点击生成时读取" },
  { value: "rules", label: "按规则：关键词或 @我 才触发" },
];

export function WechatConfigPage({ onNavigate }: WechatConfigPageProps) {
  const cfg = useWechatConfig(onNavigate);
  const [manualDir, setManualDir] = useState("");
  const [imageKeyInput, setImageKeyInput] = useState("");
  const [runtimeStatus, setRuntimeStatus] = useState<WechatRuntimeStatus | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<WechatProject | null>(null);

  useEffect(() => {
    void cfg.refreshAll();
    void api.loadWechatRuntimeStatus().then(setRuntimeStatus).catch(() => setRuntimeStatus(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (cfg.detect?.dbDir) setManualDir(cfg.detect.dbDir);
  }, [cfg.detect?.dbDir]);

  const detect = cfg.detect;
  const settings = cfg.draftSettings;
  const warnings = Array.isArray((cfg.settings as Record<string, unknown> | null)?.warnings)
    ? ((cfg.settings as Record<string, unknown>).warnings as string[])
    : [];

  // Dirty tracking is local to the page: the config-center draft is only
  // persisted by the save bar, so the status must say so before the user leaves.
  const dirty = useMemo(() => {
    if (!cfg.settings || !cfg.draftSettings) return false;
    try {
      return JSON.stringify(cfg.draftSettings) !== JSON.stringify(cfg.settings);
    } catch {
      return true;
    }
  }, [cfg.settings, cfg.draftSettings]);

  const saveStatus = cfg.saving
    ? "正在保存配置…"
    : !settings
      ? "正在读取配置…"
      : dirty
        ? "有未保存的修改，保存后才会生效。"
        : "所有修改已保存。";

  const updateStyle = (index: number, patch: { label?: string; instruction?: string }) => {
    cfg.updateReplyStyles((styles) =>
      styles.map((style, current) => (current === index ? { ...style, ...patch } : style)),
    );
  };

  return (
    <div className="wechat-config-page">
      <header className="wechat-config-hero">
        <div>
          <span className="eyebrow">PERSONAL WECHAT · CONFIG CENTER</span>
          <h1>个人微信配置中心</h1>
          <p>
            集中管理微信数据目录、项目默认值、回复风格与模型/工具。会话与草稿仍留在工作台，
            这里不再使用多层抽屉。
          </p>
        </div>
        <div className="wechat-config-hero-actions">
          <button
            type="button"
            className="button button-secondary"
            onClick={() => cfg.go("wechat-workbench")}
          >
            <ArrowLeft size={15} />返回工作台
          </button>
          <button
            type="button"
            className="button button-primary"
            disabled={cfg.detectLoading || cfg.loading}
            onClick={() => void cfg.refreshAll(true)}
          >
            {cfg.detectLoading || cfg.loading
              ? <LoaderCircle className="spin" size={15} />
              : <RefreshCw size={15} />}
            刷新检测
          </button>
        </div>
      </header>

      {cfg.error && (
        <div className="wechat-config-alert is-danger">
          <AlertCircle size={15} />
          <span>{cfg.error}</span>
        </div>
      )}
      {!cfg.backendAvailable && (
        <div className="wechat-config-alert is-warn">
          <AlertCircle size={15} />
          <span>个人微信后端接口尚未接入；本页可查看，但保存不会生效。</span>
        </div>
      )}
      {warnings.map((warning) => (
        <div className="wechat-config-alert is-warn" key={warning}>
          <AlertCircle size={15} />
          <span>{warning}</span>
        </div>
      ))}

      <div className="wechat-config-grid">
        {/* ---------------- WeChat data ---------------- */}
        <section className="wechat-config-section is-wide" aria-labelledby="wechat-config-data">
          <header className="wechat-config-section-heading">
            <span className="wechat-config-section-icon"><Database size={17} /></span>
            <div>
              <h2 id="wechat-config-data">微信数据</h2>
              <p>自动扫描本机常见目录并校验 <code>session/session.db</code>；数据库全程只读。</p>
            </div>
            <button
              type="button"
              className="button button-secondary"
              disabled={cfg.detectLoading}
              onClick={() => void cfg.loadDetection(true)}
            >
              {cfg.detectLoading ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}
              重新扫描
            </button>
          </header>

          <div className="wechat-config-status-row">
            <span className={clsx("wechat-status-chip", detect?.found ? "is-good" : "is-warn")}>
              {detect?.found ? "已检测到微信数据" : "未检测到微信数据"}
            </span>
            {detect?.running && <span className="wechat-status-chip is-good">微信运行中</span>}
            {detect?.version && <span className="wechat-status-chip is-muted">版本 {detect.version}</span>}
            {detect && (
              <span className="wechat-status-chip is-muted">
                密钥 {detect.accounts.length ? "可用" : "待扫描"} · 数据库 {detect.databaseCount ?? 0}
              </span>
            )}
            {detect?.scannedAt && <span className="wechat-config-meta">扫描于 {detect.scannedAt}</span>}
            {detect?.cached && <span className="wechat-status-chip is-muted">已复用当前目录（未全量扫描）</span>}
            {runtimeStatus && (
              <span className="wechat-status-chip is-muted" title={runtimeStatus.lastError || ""}>
                {runtimeStatus.mode === "resident"
                  ? runtimeStatus.started
                    ? "常驻 worker"
                    : "常驻 worker（待启动）"
                  : "一次性 worker"}
                {runtimeStatus.startupMs > 0 ? ` · 启动 ${runtimeStatus.startupMs}ms` : ""}
                {runtimeStatus.restarts > 0 ? ` · 重启 ${runtimeStatus.restarts} 次` : ""}
              </span>
            )}
          </div>

          {detect?.dbDir && (
            <p className="wechat-config-path" title={detect.dbDir}>
              <FolderOpen size={14} />{detect.dbDir}
            </p>
          )}

          {detect?.reason && !detect.found && (
            <div className="wechat-config-alert is-warn">
              <AlertCircle size={15} />
              <span>{detect.reason}</span>
            </div>
          )}
          {cfg.detectError && (
            <div className="wechat-config-alert is-warn">
              <AlertCircle size={15} />
              <span>{cfg.detectError}</span>
            </div>
          )}

          <div className="wechat-config-inline-actions">
            <button
              type="button"
              className="button button-primary"
              disabled={cfg.scanning || !detect?.dbDir}
              onClick={() => void cfg.scanKeys(detect?.dbDir)}
            >
              {cfg.scanning && cfg.scanningDir === detect?.dbDir
                ? <LoaderCircle className="spin" size={15} />
                : <KeyRound size={15} />}
              {cfg.scanning ? "正在验证密钥…" : "验证数据库密钥"}
            </button>
            <span className="wechat-config-meta">需要微信正在运行并登录；部分机器需要管理员权限。</span>
          </div>

          <div className="wechat-config-inline-actions">
            <Input
              className="wechat-config-manual-path"
              value={manualDir}
              placeholder="C:\\Users\\…\\xwechat_files\\wxid_…\\db_storage"
              onChange={(event) => setManualDir(event.target.value)}
            />
            <button
              type="button"
              className="button button-secondary"
              disabled={!manualDir.trim() || Boolean(cfg.selecting)}
              onClick={() => void cfg.selectAccount(manualDir.trim())}
            >
              {cfg.selecting ? "验证中…" : "使用此目录"}
            </button>
            <span className="wechat-config-meta">也可以手动指定 db_storage 路径。</span>
          </div>

          {cfg.scan && (
            <div className={clsx("wechat-config-scan", cfg.scan.ok ? "is-good" : "is-warn")}>
              {cfg.scan.ok ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
              <span>{cfg.scan.message || (cfg.scan.ok ? "密钥已验证" : "密钥扫描未通过")}</span>
            </div>
          )}

          <div className="wechat-config-imagekey">
            <div className="wechat-config-candidates-heading">
              <strong><ImageIcon size={14} /> 图片密钥</strong>
              <span>
                {detect?.imageKeyVerified
                  ? "已保存并通过真实图片验证"
                  : detect?.imageKeyConfigured
                    ? "已保存，但尚未验证"
                    : "未配置（V2 加密图片会显示占位）"}
              </span>
            </div>
            <div className="wechat-config-inline-actions">
              <button
                type="button"
                className="button button-secondary"
                disabled={cfg.imageScanning || !detect?.dbDir}
                onClick={() => void cfg.scanImageKey()}
              >
                {cfg.imageScanning ? <LoaderCircle className="spin" size={15} /> : <ImageIcon size={15} />}
                {cfg.imageScanning ? "正在扫描图片密钥…" : "扫描图片密钥"}
              </button>
              <Input
                className="wechat-config-imagekey-input"
                value={imageKeyInput}
                maxLength={16}
                placeholder="手动填写 16 位 AES Key（可留空清除）"
                onChange={(event) => setImageKeyInput(event.target.value)}
              />
              <button
                type="button"
                className="button button-secondary"
                disabled={cfg.imageKeySaving || !imageKeyInput.trim()}
                onClick={() => void cfg.saveImageKey(imageKeyInput)}
              >
                {cfg.imageKeySaving ? <LoaderCircle className="spin" size={15} /> : <KeyRound size={15} />}
                保存 Key
              </button>
              <button
                type="button"
                className="wechat-text-button"
                disabled={cfg.imageKeySaving || !detect?.imageKeyConfigured}
                onClick={() => void cfg.saveImageKey("")}
              >
                清除
              </button>
            </div>
            <span className="wechat-config-meta">
              扫描需要微信正在运行，并先在微信中打开几张普通图片；密钥只保存在本机，不会写入日志或导出文件。
            </span>
            {cfg.imageKeyResult && (
              <div className={clsx("wechat-config-scan", cfg.imageKeyResult.ok ? "is-good" : "is-warn")}>
                {cfg.imageKeyResult.ok ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
                <span>{cfg.imageKeyResult.message}</span>
              </div>
            )}
          </div>

          <div className="wechat-config-candidates">
            <div className="wechat-config-candidates-heading">
              <strong>检测候选</strong>
              <span>{detect?.candidates.length ?? 0} 个目录</span>
            </div>
            {detect?.candidates.length ? (
              detect.candidates.map((candidate) => (
                <div
                  className={clsx(
                    "wechat-config-candidate",
                    candidate.valid ? "is-valid" : "is-invalid",
                    candidate.selected && "is-selected",
                  )}
                  key={candidate.path}
                >
                  <span className="wechat-config-candidate-score">{candidate.score}</span>
                  <span className="wechat-config-candidate-copy">
                    <strong>{candidate.accountId || "未知账号"}</strong>
                    <small title={candidate.path}>{candidate.path}</small>
                    <em>{candidate.reason || candidate.source}</em>
                  </span>
                  <span className="wechat-config-candidate-actions">
                    {candidate.valid ? (
                      candidate.selected ? (
                        <span className="wechat-status-chip is-good">当前使用</span>
                      ) : (
                        <button
                          type="button"
                          className="wechat-text-button"
                          disabled={Boolean(cfg.selecting)}
                          onClick={() => void cfg.selectAccount(candidate.path)}
                        >
                          {cfg.selecting === candidate.path ? "切换中…" : "使用此目录"}
                        </button>
                      )
                    ) : (
                      <span className="wechat-status-chip is-warn">不可用</span>
                    )}
                  </span>
                </div>
              ))
            ) : (
              <p className="wechat-config-meta">
                {cfg.detectLoading ? "正在扫描常见目录…" : "尚未发现候选目录；可手动指定 db_storage 路径。"}
              </p>
            )}
            {detect?.checkedPaths.length ? (
              <details className="wechat-config-checked">
                <summary>已检查的目录（{detect.checkedPaths.length}）</summary>
                {detect.checkedPaths.map((path) => <code key={path}>{path}</code>)}
              </details>
            ) : null}
          </div>
        </section>

        {/* ---------------- Project defaults ---------------- */}
        <section className="wechat-config-section" aria-labelledby="wechat-config-defaults">
          <header className="wechat-config-section-heading">
            <span className="wechat-config-section-icon"><MessageCircle size={17} /></span>
            <div>
              <h2 id="wechat-config-defaults">项目默认设置</h2>
              <p>影响轮询频率、上下文范围与新项目的默认口吻/触发方式。</p>
            </div>
          </header>

          {settings ? (
            <div className="form-grid two-columns">
              <Field label="轮询间隔（秒）" hint="仅影响本页与工作台的本地轮询。">
                <Input
                  type="number"
                  min={1}
                  max={30}
                  value={settings.pollIntervalSeconds}
                  onChange={(event) => cfg.updateDraft({ pollIntervalSeconds: Number(event.target.value) })}
                />
              </Field>
              <Field label="近期消息条数" hint="发送给模型的上下文条数上限。">
                <Input
                  type="number"
                  min={10}
                  max={500}
                  value={settings.contextMessageLimit}
                  onChange={(event) => cfg.updateDraft({ contextMessageLimit: Number(event.target.value) })}
                />
              </Field>
              <Field label="上下文字数上限">
                <Input
                  type="number"
                  min={1000}
                  max={100000}
                  step={1000}
                  value={settings.contextCharacterLimit}
                  onChange={(event) => cfg.updateDraft({ contextCharacterLimit: Number(event.target.value) })}
                />
              </Field>
              <Field label="默认口吻" hint="创建新项目时预填，可在项目里单独修改。">
                <select
                  className="wechat-config-select"
                  value={settings.defaultTone || "自然友好"}
                  onChange={(event) => cfg.updateDraft({ defaultTone: event.target.value })}
                >
                  {TONE_PRESETS.map((tone) => <option key={tone} value={tone}>{tone}</option>)}
                </select>
              </Field>
              <Field label="默认触发方式" hint="群聊选择“按规则”时会预填关键词编辑区。" className="is-span-two">
                <select
                  className="wechat-config-select"
                  value={settings.defaultTriggerMode || ""}
                  onChange={(event) => cfg.updateDraft({ defaultTriggerMode: event.target.value as TriggerMode | "" })}
                >
                  {TRIGGER_MODE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </Field>
            </div>
          ) : (
            <p className="wechat-config-meta">{cfg.loading ? "正在读取配置…" : "尚未读取到配置。"}</p>
          )}

          <div className="wechat-config-projects">
            <div className="wechat-config-candidates-heading">
              <strong>会话项目</strong>
              <button type="button" className="wechat-text-button" onClick={cfg.openNewProject}>
                <Plus size={13} />新建项目
              </button>
            </div>
            {cfg.projects.length ? cfg.projects.map((project) => (
              <div className="wechat-config-project-row" key={project.id}>
                <span className="wechat-config-project-copy">
                  <strong>{project.name}</strong>
                  <small>
                    {project.kind === "group" ? "群聊" : "单聊"} · {project.tone || "自然友好"} ·{" "}
                    {project.trigger?.mode === "rules" ? "按规则" : project.trigger?.mode === "manual" ? "手动" : "自动"}
                  </small>
                </span>
                <button type="button" className="wechat-text-button" onClick={() => cfg.openEditProject(project)}>
                  <Settings2 size={13} />编辑
                </button>
                <button
                  type="button"
                  className="wechat-text-button is-danger"
                  onClick={() => setDeleteTarget(project)}
                >
                  <Trash2 size={13} />删除
                </button>
              </div>
            )) : (
              <p className="wechat-config-meta">还没有会话项目；可在工作台或这里新建。</p>
            )}
          </div>
        </section>

        {/* ---------------- Reply styles ---------------- */}
        <section className="wechat-config-section is-wide" aria-labelledby="wechat-config-styles">
          <header className="wechat-config-section-heading">
            <span className="wechat-config-section-icon"><Wand2 size={17} /></span>
            <div>
              <h2 id="wechat-config-styles">回复风格</h2>
              <p>草稿面板据此渲染改写按钮；名称会直接显示在按钮上，指令会追加到改写请求。</p>
            </div>
            <button type="button" className="button button-secondary" onClick={cfg.resetReplyStyles}>
              <RefreshCw size={14} />恢复默认
            </button>
          </header>

          <div className="wechat-config-styles">
            {(settings?.replyStyles?.length ? settings.replyStyles : DEFAULT_REPLY_STYLES).map((style, index) => (
              <div className="wechat-config-style-row" key={style.id}>
                <div className="wechat-config-style-head">
                  <Input
                    className="wechat-config-style-label"
                    value={style.label}
                    maxLength={24}
                    placeholder="按钮名称"
                    onChange={(event) => updateStyle(index, { label: event.target.value })}
                  />
                  <code>{style.id}</code>
                  <button
                    type="button"
                    className="wechat-icon-button"
                    title="删除风格"
                    disabled={
                      (settings?.replyStyles?.length ?? DEFAULT_REPLY_STYLES.length) <= 1
                    }
                    onClick={() => cfg.updateReplyStyles((styles) => styles.filter((_item, current) => current !== index))}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
                <textarea
                  className="wechat-config-textarea"
                  value={style.instruction}
                  maxLength={400}
                  rows={2}
                  placeholder="告诉模型如何改写这份草稿"
                  onChange={(event) => updateStyle(index, { instruction: event.target.value })}
                />
              </div>
            ))}
          </div>
          <div className="wechat-config-inline-actions">
            <button
              type="button"
              className="button button-secondary"
              disabled={(settings?.replyStyles?.length ?? DEFAULT_REPLY_STYLES.length) >= 8}
              onClick={() =>
                cfg.updateReplyStyles((styles) => [
                  ...styles,
                  {
                    id: `style-${Date.now().toString(36)}`,
                    label: "新风格",
                    instruction: "在这里描述希望模型如何改写草稿。",
                  },
                ])
              }
            >
              <Plus size={14} />添加风格
            </button>
            <span className="wechat-config-meta">最多 8 条；ID 用于兼容旧草稿与审计日志。</span>
          </div>
        </section>

        {/* ---------------- Models & tools ---------------- */}
        <section className="wechat-config-section" aria-labelledby="wechat-config-models">
          <header className="wechat-config-section-heading">
            <span className="wechat-config-section-icon"><Bot size={17} /></span>
            <div>
              <h2 id="wechat-config-models">模型与只读 MCP</h2>
              <p>这里只读展示；模型连接在全局设置中编辑，工具授权在项目编辑里选择。</p>
            </div>
            <button type="button" className="button button-secondary" onClick={() => cfg.go("settings")}>
              <Settings2 size={14} />全局设置
            </button>
          </header>

          <span className="wechat-form-label">模型连接</span>
          <div className="wechat-config-card-list">
            {cfg.providers.length ? cfg.providers.map((provider) => (
              <div className="wechat-config-card" key={provider.id}>
                <Bot size={16} />
                <span>
                  <strong>{provider.name}</strong>
                  <small>{provider.model || "未填写模型"}{provider.baseUrl ? ` · ${provider.baseUrl}` : ""}</small>
                </span>
                <span className={clsx("wechat-status-chip", provider.hasSecret ? "is-good" : "is-muted")}>
                  {provider.hasSecret ? "密钥已保存" : "密钥待配置"}
                </span>
              </div>
            )) : (
              <p className="wechat-config-meta">{cfg.loading ? "正在读取模型配置…" : "尚未配置模型；请到全局设置添加。"}</p>
            )}
          </div>

          <span className="wechat-form-label">只读 MCP 工具</span>
          <div className="wechat-config-card-list">
            {cfg.mcpServers.length ? cfg.mcpServers.map((server) => (
              <div className="wechat-config-card" key={server.id}>
                <Network size={16} />
                <span>
                  <strong>{server.name}</strong>
                  <small>
                    {server.toolCount} 个工具 · {server.transport || "本地"} · {server.enabled ? "已启用" : "已停用"}
                  </small>
                </span>
                {server.lastError
                  ? <span className="wechat-status-chip is-warn" title={server.lastError}>最近失败</span>
                  : <span className="wechat-status-chip is-muted">只读</span>}
              </div>
            )) : (
              <p className="wechat-config-meta">
                {cfg.loading ? "正在读取 MCP 配置…" : "尚未配置 MCP 服务；可在全局 MCP 页面添加。"}
              </p>
            )}
          </div>
        </section>

        {/* ---------------- Export & advanced ---------------- */}
        <section className="wechat-config-section" aria-labelledby="wechat-config-advanced">
          <header className="wechat-config-section-heading">
            <span className="wechat-config-section-icon"><Sparkles size={17} /></span>
            <div>
              <h2 id="wechat-config-advanced">导出与高级</h2>
              <p>导出为带 media 目录的 Markdown/JSON；高级选项只影响本机数据。</p>
            </div>
          </header>

          <div className="wechat-config-inline-actions">
            <button type="button" className="button button-secondary" onClick={() => cfg.go("wechat-export")}>
              <Download size={15} />导出会话
            </button>
            <button
              type="button"
              className="button button-secondary"
              disabled={cfg.importing}
              onClick={() => void cfg.importLegacy()}
            >
              {cfg.importing ? <LoaderCircle className="spin" size={15} /> : <Import size={15} />}
              导入 ProxyTalk 旧数据
            </button>
          </div>
          {cfg.importResult && (
            <div className={clsx("wechat-config-scan", cfg.importResult.imported ? "is-good" : "is-warn")}>
              {cfg.importResult.imported ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
              <span>{cfg.importResult.message}</span>
            </div>
          )}

          {settings && (
            <>
              <Field label="图片搜索目录" hint="每行一个目录；用于解析微信图片缓存，留空则只用默认位置。">
                <textarea
                  className="wechat-config-textarea"
                  rows={3}
                  value={(settings.imageSearchRoots as string[] | undefined)?.join("\n") ?? ""}
                  onChange={(event) => cfg.updateDraft({
                    imageSearchRoots: event.target.value
                      .split(/\r?\n/)
                      .map((line) => line.trim())
                      .filter(Boolean),
                  })}
                />
              </Field>
              <Switch
                checked={Boolean(settings.voiceTranscription)}
                onChange={(checked) => cfg.updateDraft({ voiceTranscription: checked })}
                label="启用本地语音转录"
                description="调用本机命令把语音转成文字，不发送到云端。"
              />
              {Boolean(settings.voiceTranscription) && (
                <div className="form-grid two-columns">
                  <Field label="转录命令" hint="可执行文件路径或 PATH 中的命令。">
                    <Input
                      value={(settings.voiceCommand as string) || ""}
                      onChange={(event) => cfg.updateDraft({ voiceCommand: event.target.value })}
                    />
                  </Field>
                  <Field label="模型路径">
                    <Input
                      value={(settings.voiceModelPath as string) || ""}
                      onChange={(event) => cfg.updateDraft({ voiceModelPath: event.target.value })}
                    />
                  </Field>
                </div>
              )}
              <details className="wechat-config-checked">
                <summary>数据位置与凭据模式</summary>
                <code>数据根：{(settings.dataRoot as string) || "未提供"}</code>
                <code>状态库：{(settings.database as string) || "未提供"}</code>
                <code>凭据模式：{settings.vaultMode === "dpapi" ? "DPAPI（本机用户加密）" : settings.vaultMode === "memory" ? "内存（重启后需重填）" : "未知"}</code>
              </details>
            </>
          )}
        </section>
      </div>

      <footer className="wechat-config-savebar">
        <span id="wechat-config-save-status" role="status" aria-live="polite" aria-atomic="true">
          {saveStatus}
        </span>
        <button
          type="button"
          className="button button-primary"
          disabled={cfg.saving || !settings}
          aria-busy={cfg.saving}
          aria-describedby="wechat-config-save-status"
          onClick={() => void cfg.saveSettings()}
        >
          {cfg.saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
          {cfg.saving ? "保存中" : "保存配置"}
        </button>
      </footer>

      {cfg.editor && (
        <ProjectEditorDrawer
          state={cfg.editor}
          chats={cfg.chats}
          chatsLoading={cfg.chatsLoading}
          chatsError={cfg.chatsError}
          providers={cfg.providers}
          mcpServers={cfg.mcpServers}
          projectDefaults={{
            tone: settings?.defaultTone,
            triggerMode: (settings?.defaultTriggerMode || undefined) as TriggerMode | undefined,
          }}
          saving={cfg.savingProject}
          onClose={cfg.closeEditor}
          onReloadChats={() => void cfg.loadChats()}
          onSave={cfg.saveProject}
          onDelete={(project) => setDeleteTarget(project)}
        />
      )}

      {deleteTarget && (
        <ConfirmDialog
          request={{
            title: "删除会话项目？",
            message: `将删除“${deleteTarget.name}”在本工具中的项目、本地消息缓存、草稿和同步游标。微信原始聊天记录不会被修改。`,
            confirmLabel: "删除项目",
            tone: "danger",
            onConfirm: () => {
              const target = deleteTarget;
              setDeleteTarget(null);
              void cfg.deleteProject(target);
            },
          }}
          onClose={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}

export default WechatConfigPage;

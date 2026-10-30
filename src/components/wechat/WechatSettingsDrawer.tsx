import { useEffect, useId, useState } from "react";
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  Database,
  KeyRound,
  LoaderCircle,
  Network,
  RefreshCw,
  Save,
  ShieldCheck,
  X,
} from "lucide-react";
import clsx from "clsx";

import { Field, Input } from "../ui";
import { useDialogA11y } from "../../lib/useDialogA11y";
import { globalModelSummary } from "../../pages/wechat/logic";
import type {
  WechatDetectResult,
  WechatLlmConfig,
  WechatMcpServer,
  WechatProvider,
  WechatScanResult,
  WechatSettings,
} from "../../pages/wechat/types";

type SettingsTab = "wechat" | "models" | "runtime";

export interface WechatSettingsDrawerProps {
  detect: WechatDetectResult | null;
  detectLoading: boolean;
  detectError: string;
  scan: WechatScanResult | null;
  scanning: boolean;
  settings: WechatSettings | null;
  providers: WechatProvider[];
  llm: WechatLlmConfig;
  mcpServers: WechatMcpServer[];
  configLoading: boolean;
  configError: string;
  savingSettings: boolean;
  backendAvailable: boolean;
  selectingAccount: string;
  onClose: () => void;
  onDetect: () => void;
  onScanKeys: (dbDir?: string) => void;
  onSelectAccount: (dbDir: string) => void;
  onReloadConfig: () => void;
  onSaveSettings: (settings: WechatSettings) => Promise<void>;
}

export function WechatSettingsDrawer({
  detect,
  detectLoading,
  detectError,
  scan,
  scanning,
  settings,
  providers,
  llm,
  mcpServers,
  configLoading,
  configError,
  savingSettings,
  backendAvailable,
  selectingAccount,
  onClose,
  onDetect,
  onScanKeys,
  onSelectAccount,
  onReloadConfig,
  onSaveSettings,
}: WechatSettingsDrawerProps) {
  const [tab, setTab] = useState<SettingsTab>("wechat");
  const [dbDir, setDbDir] = useState("");
  const [draftSettings, setDraftSettings] = useState<WechatSettings | null>(null);
  const titleId = useId();
  const panelRef = useDialogA11y<HTMLElement>({
    open: true,
    onClose,
    // Saving runtime preferences must not be dismissible by Escape.
    closeDisabled: savingSettings,
    initialFocusWithin: ".wechat-drawer-body",
  });

  useEffect(() => {
    setDbDir(settings?.dbDir || detect?.dbDir || "");
  }, [settings?.dbDir, detect?.dbDir]);

  useEffect(() => {
    setDraftSettings(settings ? structuredClone(settings) : null);
  }, [settings]);

  const updateSetting = <K extends keyof WechatSettings>(key: K, value: WechatSettings[K]) => {
    setDraftSettings((current) => (current ? { ...current, [key]: value } : current));
  };

  return (
    <div className="modal-backdrop">
      <section
        ref={panelRef}
        className="wechat-drawer wechat-settings-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <header className="wechat-drawer-header">
          <div>
            <span className="eyebrow">LOCAL CONTROL ROOM</span>
            <h2 id={titleId}>设置与连接</h2>
          </div>
          <button
            type="button"
            className="wechat-icon-button"
            aria-label="关闭"
            disabled={savingSettings}
            onClick={onClose}
          >
            <X size={17} />
          </button>
        </header>

        <nav className="wechat-settings-tabs" aria-label="设置分区">
          <button type="button" className={clsx(tab === "wechat" && "is-active")} onClick={() => setTab("wechat")}>
            <Database size={15} />微信数据
          </button>
          <button type="button" className={clsx(tab === "models" && "is-active")} onClick={() => setTab("models")}>
            <Bot size={15} />模型与工具
          </button>
          <button type="button" className={clsx(tab === "runtime" && "is-active")} onClick={() => setTab("runtime")}>
            <RefreshCw size={15} />运行偏好
          </button>
        </nav>

        <div className="wechat-drawer-body wechat-settings-body">
          {!backendAvailable && (
            <div className="wechat-inline-error">
              <AlertCircle size={15} />
              <span>个人微信后端接口尚未接入；设置可查看，但保存不会生效。</span>
            </div>
          )}

          {tab === "wechat" && (
            <div className="wechat-settings-page">
              <div className="wechat-settings-heading">
                <h3>微信数据源</h3>
                <p>原始数据库只读；扫描密钥需要微信正在运行，部分机器需要管理员权限。</p>
              </div>

              <div className={clsx("wechat-source-card", detect?.found ? "is-success" : "is-warning")}>
                <span className="wechat-source-dot" />
                <span>
                  <strong>{detect?.found ? "已检测到微信数据目录" : "尚未检测到微信"}</strong>
                  <small title={detect?.dbDir}>{detect?.dbDir || "请确认微信已安装并登录后重新检测"}</small>
                </span>
                {detect?.running && <span className="wechat-status-chip is-good">微信运行中</span>}
              </div>

              <Field label="微信数据目录" hint="通常自动检测；也可手动填写 db_storage 目录后重新扫描。">
                <Input value={dbDir} onChange={(event) => setDbDir(event.target.value)} placeholder="C:\\…\\xwechat_files\\…\\db_storage" />
              </Field>

              <div className="wechat-settings-actions">
                <button type="button" className="button button-secondary" onClick={onDetect} disabled={detectLoading}>
                  {detectLoading ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}
                  重新检测
                </button>
                <button
                  type="button"
                  className="button button-primary"
                  disabled={scanning || (!dbDir && !detect?.dbDir)}
                  onClick={() => onScanKeys(dbDir || detect?.dbDir || undefined)}
                >
                  {scanning ? <LoaderCircle className="spin" size={15} /> : <KeyRound size={15} />}
                  {scanning ? "正在扫描密钥…" : "扫描并验证数据库密钥"}
                </button>
              </div>

              {detectError && (
                <div className="wechat-inline-error"><AlertCircle size={14} /><span>{detectError}</span></div>
              )}
              {scan && (
                <div className={clsx("wechat-scan-result", scan.ok ? "is-good" : "is-warn")}>
                  {scan.ok ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
                  <span>{scan.message || (scan.ok ? "密钥已验证" : "密钥扫描未通过")}</span>
                </div>
              )}

              {detect?.accounts.length ? (
                <div className="wechat-account-list">
                  <span className="wechat-form-label">检测到的微信账号</span>
                  {detect.accounts.map((account) => (
                    <div key={`${account.accountId}:${account.dbDir}`} className="wechat-account-row">
                      <span><strong>{account.accountId || "微信账号"}</strong><small title={account.dbDir}>{account.dbDir || "未提供目录"}</small></span>
                      {account.selected
                        ? <span className="wechat-status-chip is-good">已选择</span>
                        : account.dbDir
                          ? (
                            <button
                              type="button"
                              className="wechat-text-button"
                              disabled={Boolean(selectingAccount)}
                              onClick={() => onSelectAccount(account.dbDir)}
                            >
                              {selectingAccount === account.dbDir ? "切换中…" : "使用此账号"}
                            </button>
                          )
                          : null}
                    </div>
                  ))}
                </div>
              ) : null}

              <p className="wechat-settings-note">
                <ShieldCheck size={14} />密钥只以本机用户可解密的密文保存，不写入日志或导出文件。
              </p>
            </div>
          )}

              {tab === "models" && (
            <div className="wechat-settings-page">
              <div className="wechat-settings-heading">
                <h3>模型与工具</h3>
                <p>模型连接以全局设置为准；这里只读展示当前生效模型与可用的 MCP 服务。</p>
              </div>
              {configError && (
                <div className="wechat-inline-error"><AlertCircle size={14} /><span>{configError}</span></div>
              )}
              <div className="wechat-config-list">
                <span className="wechat-form-label">当前模型</span>
                {(() => {
                  const globalModel = globalModelSummary(llm, providers);
                  return (
                    <div className={clsx("wechat-config-card", globalModel.configured && "is-global")}>
                      <Bot size={16} />
                      <span>
                        <strong>{globalModel.name}</strong>
                        <small>
                          {globalModel.configured
                            ? `${globalModel.model}${globalModel.baseUrl ? ` · ${globalModel.baseUrl}` : ""}`
                            : "尚未配置模型；请到全局设置添加。"}
                        </small>
                      </span>
                      <span className={clsx("wechat-status-chip", globalModel.fromCache ? "is-warn" : "is-muted")}>
                        {globalModel.sourceLabel}
                      </span>
                    </div>
                  );
                })()}
              </div>
              <div className="wechat-config-list">
                <span className="wechat-form-label">MCP 服务</span>
                {mcpServers.length ? mcpServers.map((server) => (
                  <div key={server.id} className="wechat-config-card">
                    <Network size={16} />
                    <span>
                      <strong>{server.name}</strong>
                      <small>{server.toolCount} 个工具 · {server.enabled ? "已启用" : "已停用"}</small>
                    </span>
                    {server.lastError ? <span className="wechat-status-chip is-warn">最近失败</span> : <span className="wechat-status-chip is-muted">{server.transport || "本地"}</span>}
                  </div>
                )) : (
                  <p className="wechat-form-hint">{configLoading ? "正在读取 MCP 配置…" : "尚未配置 MCP 服务。"}</p>
                )}
              </div>
              <button type="button" className="button button-secondary" onClick={onReloadConfig} disabled={configLoading}>
                {configLoading ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}刷新配置
              </button>
            </div>
          )}

          {tab === "runtime" && (
            <div className="wechat-settings-page">
              <div className="wechat-settings-heading">
                <h3>运行偏好</h3>
                <p>这些设置只影响本机轮询与发送给模型的近期上下文。</p>
              </div>
              {draftSettings ? (
                <>
                  <div className="form-grid two-columns">
                    <Field label="轮询间隔（秒）">
                      <Input
                        type="number"
                        min={1}
                        max={30}
                        value={draftSettings.pollIntervalSeconds}
                        onChange={(event) => updateSetting("pollIntervalSeconds", Number(event.target.value))}
                      />
                    </Field>
                    <Field label="近期消息条数">
                      <Input
                        type="number"
                        min={10}
                        max={500}
                        value={draftSettings.contextMessageLimit}
                        onChange={(event) => updateSetting("contextMessageLimit", Number(event.target.value))}
                      />
                    </Field>
                    <Field label="上下文字数上限">
                      <Input
                        type="number"
                        min={1000}
                        max={100000}
                        step={1000}
                        value={draftSettings.contextCharacterLimit}
                        onChange={(event) => updateSetting("contextCharacterLimit", Number(event.target.value))}
                      />
                    </Field>
                  </div>
                  <button
                    type="button"
                    className="button button-primary"
                    disabled={savingSettings}
                    onClick={() => void onSaveSettings(draftSettings)}
                  >
                    {savingSettings ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
                    {savingSettings ? "保存中" : "保存运行偏好"}
                  </button>
                </>
              ) : (
                <p className="wechat-form-hint">{configLoading ? "正在读取运行偏好…" : "尚未读取到运行偏好。"}</p>
              )}
            </div>
          )}
        </div>

        <footer className="wechat-drawer-footer">
          <span />
          <button type="button" className="button button-secondary" disabled={savingSettings} onClick={onClose}>完成</button>
        </footer>
      </section>
    </div>
  );
}

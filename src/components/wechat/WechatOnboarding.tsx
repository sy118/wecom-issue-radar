import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  Database,
  KeyRound,
  LoaderCircle,
  MessageCircle,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import clsx from "clsx";

import type {
  WechatDetectResult,
  WechatScanResult,
} from "../../pages/wechat/types";

export interface WechatOnboardingProps {
  detect: WechatDetectResult | null;
  detectLoading: boolean;
  detectError: string;
  scan: WechatScanResult | null;
  scanning: boolean;
  backendAvailable: boolean;
  projectCount: number;
  onDetect: () => void;
  onScanKeys: (dbDir?: string) => void;
  onCreateProject: () => void;
  onOpenSettings: () => void;
  onNavigate?: (page: string) => void;
}

export function WechatOnboarding({
  detect,
  detectLoading,
  detectError,
  scan,
  scanning,
  backendAvailable,
  projectCount,
  onDetect,
  onScanKeys,
  onCreateProject,
  onOpenSettings,
  onNavigate,
}: WechatOnboardingProps) {
  if (projectCount > 0) {
    return (
      <main className="wechat-conversation wechat-conversation-empty">
        <MessageCircle size={30} />
        <strong>选择一个会话项目</strong>
        <span>左侧项目会读取本地微信消息，并在右侧保留可编辑的回复草稿。</span>
      </main>
    );
  }

  const detected = Boolean(detect?.found);
  const scanned = Boolean(scan?.ok);
  // Detection is enough to open the chat picker; a project save surfaces any
  // remaining key/permission problem with an actionable error.
  const canCreate = detected;

  return (
    <main className="wechat-onboarding">
      <div className="wechat-onboarding-card">
        <header className="wechat-onboarding-header">
          <span className="wechat-onboarding-mark"><Sparkles size={22} /></span>
          <div>
            <span className="eyebrow">PERSONAL WECHAT · FIRST RUN</span>
            <h2>连接个人微信，生成可审核的回复草稿</h2>
            <p>只读取已解锁的本地数据库；所有草稿都要你复制或填入后手动发送。</p>
          </div>
        </header>

        {!backendAvailable && (
          <div className="wechat-onboarding-warning">
            <AlertCircle size={16} />
            <span>
              <strong>个人微信后端尚未接入</strong>
              当前版本缺少 <code>wechatAction</code> 桥接；更新应用后即可完成检测、扫描与项目创建。
            </span>
          </div>
        )}

        <ol className="wechat-onboarding-steps">
          <li className={clsx(detected && "is-done")}>
            <span className="wechat-step-index">{detected ? <CheckCircle2 size={16} /> : "1"}</span>
            <div className="wechat-step-body">
              <div className="wechat-step-heading">
                <strong>检测微信数据目录</strong>
                {detected
                  ? <span className="wechat-status-chip is-good">已检测到</span>
                  : <span className="wechat-status-chip is-muted">待检测</span>}
              </div>
              <p>确认微信正在运行并登录；应用只读取 <code>db_storage</code>，不会修改原始数据库。</p>
              {detect?.dbDir && <p className="wechat-step-path" title={detect.dbDir}><Database size={13} />{detect.dbDir}</p>}
              {detect?.version && <p className="wechat-step-meta">微信版本：{detect.version}{detect.running ? " · 运行中" : ""}</p>}
              {detectError && (
                <div className="wechat-inline-error">
                  <AlertCircle size={14} /><span>{detectError}</span>
                </div>
              )}
              <div className="wechat-step-actions">
                <button type="button" className="button button-secondary" onClick={onDetect} disabled={detectLoading}>
                  {detectLoading ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}
                  {detected ? "重新检测" : "检测微信目录"}
                </button>
              </div>
            </div>
          </li>

          <li className={clsx(scanned && "is-done")}>
            <span className="wechat-step-index">{scanned ? <CheckCircle2 size={16} /> : "2"}</span>
            <div className="wechat-step-body">
              <div className="wechat-step-heading">
                <strong>扫描并验证数据库密钥</strong>
                {scanned
                  ? <span className="wechat-status-chip is-good">已验证</span>
                  : <span className="wechat-status-chip is-muted">待扫描</span>}
              </div>
              <p>密钥从运行中的微信进程内存安全提取，只保存在本机；部分机器需要管理员权限。</p>
              {scan && (
                <div className={clsx("wechat-scan-result", scan.ok ? "is-good" : "is-warn")}>
                  {scan.ok ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
                  <span>{scan.message || (scan.ok ? "密钥已验证" : "扫描未通过")}</span>
                </div>
              )}
              <div className="wechat-step-actions">
                <button
                  type="button"
                  className="button button-primary"
                  disabled={!detected || scanning}
                  onClick={() => onScanKeys(detect?.dbDir || undefined)}
                >
                  {scanning ? <LoaderCircle className="spin" size={15} /> : <KeyRound size={15} />}
                  {scanning ? "正在扫描…" : "扫描密钥"}
                </button>
                <button type="button" className="button button-secondary" onClick={onOpenSettings}>
                  <Settings2 size={15} />数据与连接设置
                </button>
              </div>
            </div>
          </li>

          <li className={clsx(projectCount > 0 && "is-done")}>
            <span className="wechat-step-index">3</span>
            <div className="wechat-step-body">
              <div className="wechat-step-heading">
                <strong>创建第一个会话项目</strong>
                <span className="wechat-status-chip is-muted">选择会话 · 摘要 · 口吻 · 触发规则</span>
              </div>
              <p>项目把会话、回复口吻与模型/Skill/MCP 绑定在一起；同一个会话建议只建一个项目。</p>
              <div className="wechat-step-actions">
                <button
                  type="button"
                  className="button button-primary"
                  disabled={!canCreate}
                  onClick={onCreateProject}
                >
                  <MessageCircle size={15} />创建会话项目
                </button>
                {!canCreate && <span className="wechat-step-hint">先完成第一步“检测微信目录”。</span>}
              </div>
            </div>
          </li>
        </ol>

        <footer className="wechat-onboarding-footer">
          <p className="wechat-safety-note">
            <ShieldCheck size={14} />
            <span><strong>安全边界：</strong>回复只会被复制或填入微信空白输入框，应用不会点击发送，也不会自动发送。</span>
          </p>
          <div className="wechat-onboarding-links">
            <button type="button" onClick={onOpenSettings}>微信数据与连接</button>
            {onNavigate && (
              <button type="button" onClick={() => onNavigate("settings")}>
                全局模型与工具设置<ArrowRight size={13} />
              </button>
            )}
          </div>
        </footer>
      </div>
    </main>
  );
}

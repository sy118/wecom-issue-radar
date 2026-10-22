import {
  AlertCircle,
  Download,
  LoaderCircle,
  MessageCircle,
  Plus,
  RefreshCw,
  Settings2,
  Users,
} from "lucide-react";
import clsx from "clsx";

import type { WechatDetectResult, WechatProject } from "../../pages/wechat/types";

export interface ProjectRailProps {
  projects: WechatProject[];
  selectedId: string;
  detect: WechatDetectResult | null;
  detectLoading: boolean;
  backendAvailable: boolean;
  loading: boolean;
  error: string;
  unreadFor: (project: WechatProject) => number;
  onSelect: (projectId: string) => void;
  onCreate: () => void;
  onRefresh: () => void;
  onExport: () => void;
  onOpenConfig: () => void;
}

export function ProjectRail({
  projects,
  selectedId,
  detect,
  detectLoading,
  backendAvailable,
  loading,
  error,
  unreadFor,
  onSelect,
  onCreate,
  onRefresh,
  onExport,
  onOpenConfig,
}: ProjectRailProps) {
  const status = !backendAvailable
    ? { tone: "danger", label: "后端未接入", hint: "请更新应用后再使用个人微信" }
    : detectLoading
      ? { tone: "progress", label: "正在检测", hint: "查找本机微信数据目录" }
      : detect?.found
        ? { tone: "success", label: detect.running ? "监听中" : "已检测到微信", hint: detect.dbDir || "数据目录已就绪" }
        : { tone: "warning", label: "未检测到微信", hint: "请确认微信已安装并登录" };

  return (
    <aside className="wechat-rail" aria-label="个人微信项目栏">
      <header className="wechat-rail-heading">
        <div>
          <span className="eyebrow">PERSONAL WECHAT</span>
          <h1>会话项目</h1>
        </div>
        <div className="wechat-rail-actions">
          <button
            type="button"
            className="wechat-icon-button"
            title="配置中心"
            onClick={onOpenConfig}
          >
            <Settings2 size={16} />
          </button>
          <button type="button" className="wechat-icon-button" title="刷新项目" onClick={onRefresh} disabled={loading}>
            <RefreshCw size={16} className={loading ? "spin" : undefined} />
          </button>
          <button type="button" className="wechat-icon-button" title="新建会话项目" onClick={onCreate}>
            <Plus size={18} />
          </button>
        </div>
      </header>

      <div className={clsx("wechat-source-card", `is-${status.tone}`)}>
        <span className="wechat-source-dot" />
        <span>
          <strong>{status.label}</strong>
          <small title={status.hint}>{status.hint}</small>
        </span>
        {!backendAvailable && <AlertCircle size={15} />}
      </div>

      {error && (
        <div className="wechat-rail-error">
          <AlertCircle size={14} />
          <span>{error}</span>
          <button type="button" onClick={onRefresh}>重试</button>
        </div>
      )}

      <nav className="wechat-project-list" aria-label="会话项目">
        {projects.map((project) => {
          const unread = unreadFor(project);
          return (
            <button
              type="button"
              key={project.id}
              className={clsx("wechat-project-item", project.id === selectedId && "is-active")}
              onClick={() => onSelect(project.id)}
            >
              <span className={clsx("wechat-project-avatar", `is-${project.kind}`)}>
                {project.kind === "group" ? <Users size={17} /> : <MessageCircle size={17} />}
              </span>
              <span className="wechat-project-copy">
                <strong>{project.name}</strong>
                <small>{project.kind === "group" ? "群聊" : "单聊"} · {project.chatTitle || project.chatId}</small>
              </span>
              <span className="wechat-project-meta">
                {unread > 0
                  ? <span className="wechat-unread-badge">{unread > 99 ? "99+" : unread}</span>
                  : <span className={clsx("wechat-project-dot", project.enabled && "is-on")} />}
              </span>
            </button>
          );
        })}
        {!projects.length && (
          <div className="wechat-rail-empty">
            {loading ? <LoaderCircle className="spin" size={22} /> : <MessageCircle size={24} />}
            <strong>{loading ? "正在读取项目" : "还没有会话项目"}</strong>
            <span>{loading ? "请稍候…" : "选择一个微信会话，生成可审核的回复草稿。"}</span>
            {!loading && (
              <button type="button" className="button button-secondary" onClick={onCreate}>
                <Plus size={15} />创建第一个项目
              </button>
            )}
          </div>
        )}
      </nav>

      <footer className="wechat-rail-footer">
        <button type="button" onClick={onExport}><Download size={15} /><span>导出会话</span></button>
        <button type="button" onClick={onOpenConfig}><Settings2 size={15} /><span>配置中心</span></button>
      </footer>
    </aside>
  );
}

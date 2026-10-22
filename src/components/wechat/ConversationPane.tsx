import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  AlertCircle,
  ChevronUp,
  Download,
  FileText,
  Image as ImageIcon,
  ImageOff,
  Link2,
  LoaderCircle,
  MessageCircle,
  Mic2,
  PencilLine,
  Quote,
  RefreshCw,
  Users,
} from "lucide-react";
import clsx from "clsx";

import {
  formatClock,
  formatDayLabel,
} from "../../pages/wechat/logic";
import type {
  MessageKind,
  WechatMediaPreview,
  WechatMessage,
  WechatProject,
} from "../../pages/wechat/types";

export interface ConversationPaneProps {
  project?: WechatProject;
  projectCount: number;
  messages: WechatMessage[];
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  error: string;
  onLoadMore: () => void;
  onRefresh: () => void;
  onEdit: () => void;
  onExport: () => void;
  onCreate: () => void;
  onLoadImage: (projectId: string, messageId: string) => Promise<WechatMediaPreview>;
}

const kindIcon = (kind: MessageKind) => {
  if (kind === "image") return <ImageIcon size={13} />;
  if (kind === "voice") return <Mic2 size={13} />;
  if (kind === "link" || kind === "file") return <Link2 size={13} />;
  if (kind === "quote") return <Quote size={13} />;
  if (kind === "video") return <FileText size={13} />;
  return null;
};

export function ConversationPane({
  project,
  projectCount,
  messages,
  loading,
  loadingMore,
  hasMore,
  error,
  onLoadMore,
  onRefresh,
  onEdit,
  onExport,
  onCreate,
  onLoadImage,
}: ConversationPaneProps) {
  const streamRef = useRef<HTMLDivElement>(null);
  // Keep the newest message in view for new loads, but never yank the user back
  // down while they are reading older messages.
  const stickToBottom = useRef(true);
  const previousStream = useRef({ projectId: "", count: 0, scrollHeight: 0 });

  const handleStreamScroll = () => {
    const element = streamRef.current;
    if (!element) return;
    stickToBottom.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < 48;
  };

  useLayoutEffect(() => {
    const element = streamRef.current;
    if (!element) return;
    const projectId = project?.id ?? "";
    const previous = previousStream.current;
    const projectChanged = previous.projectId !== projectId;
    const grew = messages.length > previous.count;
    if (projectChanged) {
      element.scrollTop = element.scrollHeight;
      stickToBottom.current = true;
    } else if (grew && !stickToBottom.current && previous.scrollHeight > 0) {
      // Older messages were prepended: keep the current visual anchor.
      element.scrollTop += element.scrollHeight - previous.scrollHeight;
    } else if (stickToBottom.current || previous.count === 0) {
      element.scrollTop = element.scrollHeight;
    }
    previousStream.current = {
      projectId,
      count: messages.length,
      scrollHeight: element.scrollHeight,
    };
  }, [messages, project?.id]);

  if (!project) {
    return (
      <main className="wechat-conversation wechat-conversation-empty">
        <MessageCircle size={30} />
        <strong>{projectCount ? "选择一个会话项目" : "还没有可用的会话项目"}</strong>
        <span>{projectCount
          ? "左侧选择项目后会读取本地消息；右侧同时保留可编辑的回复草稿。"
          : "先完成微信检测与密钥扫描，再选择一个会话创建项目。"}</span>
        {!projectCount && <button type="button" className="button button-secondary" onClick={onCreate}>创建会话项目</button>}
      </main>
    );
  }

  return (
    <main className="wechat-conversation">
      <header className="wechat-conversation-header">
        <div className="wechat-conversation-identity">
          <span className={clsx("wechat-conversation-avatar", `is-${project.kind}`)}>
            {project.kind === "group" ? <Users size={19} /> : project.chatTitle.slice(0, 1) || "微"}
          </span>
          <div>
            <span className="eyebrow">{project.kind === "group" ? "GROUP THREAD" : "DIRECT THREAD"}</span>
            <h2>{project.chatTitle || project.name}</h2>
            <small>{project.summary || "尚未填写项目摘要；可在项目设置中补充背景与口吻。"}</small>
          </div>
        </div>
        <div className="wechat-conversation-actions">
          <span className={clsx("wechat-status-chip", project.enabled ? "is-good" : "is-muted")}>
            {project.enabled ? "已启用" : "已停用"}
          </span>
          <button type="button" className="wechat-icon-button" title="刷新消息" onClick={onRefresh} disabled={loading}>
            <RefreshCw size={16} className={loading ? "spin" : undefined} />
          </button>
          <button type="button" className="wechat-icon-button" title="编辑项目" onClick={onEdit}>
            <PencilLine size={16} />
          </button>
          <button type="button" className="button button-secondary" onClick={onExport}>
            <Download size={15} />导出
          </button>
        </div>
      </header>

      {error && (
        <div className="wechat-inline-error">
          <AlertCircle size={15} />
          <span>{error}</span>
          <button type="button" onClick={onRefresh}>重新读取</button>
        </div>
      )}

      <div className="wechat-message-stream" aria-label="会话消息" ref={streamRef} onScroll={handleStreamScroll}>
        {hasMore && (
          <div className="wechat-load-more">
            <button type="button" className="button button-secondary" disabled={loadingMore} onClick={onLoadMore}>
              {loadingMore ? <LoaderCircle className="spin" size={14} /> : <ChevronUp size={14} />}
              {loadingMore ? "正在加载更早消息…" : "加载更早消息"}
            </button>
          </div>
        )}

        {loading && !messages.length && (
          <div className="wechat-stream-state">
            <LoaderCircle className="spin" size={24} />
            <strong>正在读取本地会话</strong>
            <span>只读取已解锁的微信数据库，不会修改原始记录。</span>
          </div>
        )}

        {!loading && !messages.length && (
          <div className="wechat-stream-state">
            <MessageCircle size={26} />
            <strong>还没有读取到消息</strong>
            <span>确认微信正在运行且在项目设置中选择了正确会话，然后重新读取。</span>
            <button type="button" className="button button-secondary" onClick={onRefresh}>
              <RefreshCw size={14} />重新读取
            </button>
          </div>
        )}

        {messages.map((message, index) => {
          const previous = messages[index - 1];
          const showDay = !previous
            || formatDayLabel(previous.createTime) !== formatDayLabel(message.createTime);
          if (message.direction === "system" || message.kind === "system") {
            return (
              <div key={message.id}>
                {showDay && <div className="wechat-day-separator"><span>{formatDayLabel(message.createTime)}</span></div>}
                <div className="wechat-system-message">{message.text || "系统消息"}</div>
              </div>
            );
          }
          const outgoing = message.direction === "outgoing";
          const isImage = message.kind === "image";
          return (
            <div key={message.id}>
              {showDay && <div className="wechat-day-separator"><span>{formatDayLabel(message.createTime)}</span></div>}
              <article className={clsx("wechat-message-row", outgoing && "is-outgoing")}>
                <div className="wechat-message-meta">
                  <strong>{outgoing ? "我" : message.senderName || "对方"}</strong>
                  <time>{formatClock(message.createTime)}</time>
                  {message.mentionedMe && <span className="wechat-mention">@我</span>}
                </div>
                <div className={clsx("wechat-message-bubble", isImage && "is-image")}>
                  {kindIcon(message.kind) && <span className="wechat-message-kind">{kindIcon(message.kind)}</span>}
                  <div className="wechat-message-content">
                    {isImage && (
                      <MessageImage
                        projectId={project.id}
                        message={message}
                        onLoadImage={onLoadImage}
                      />
                    )}
                    {(!isImage || (message.text && message.text !== "[图片]")) && (
                      <p>{message.text || `[${message.kind}]`}</p>
                    )}
                    {message.quote && (
                      <blockquote className="wechat-message-quote">
                        <strong>{message.quote.sender || "引用"}</strong>
                        <span>{message.quote.text}</span>
                      </blockquote>
                    )}
                  </div>
                </div>
              </article>
            </div>
          );
        })}
      </div>
    </main>
  );
}

function MessageImage({
  projectId,
  message,
  onLoadImage,
}: {
  projectId: string;
  message: WechatMessage;
  onLoadImage: (projectId: string, messageId: string) => Promise<WechatMediaPreview>;
}) {
  const frameRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(
    typeof IntersectionObserver === "undefined",
  );
  const [preview, setPreview] = useState<WechatMediaPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState("");

  useEffect(() => {
    const frame = frameRef.current;
    if (!frame || typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setVisible(true);
        observer.disconnect();
      }
    }, { rootMargin: "300px 0px" });
    observer.observe(frame);
    return () => observer.disconnect();
  }, [message.id]);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    setLoading(true);
    setFailure("");
    onLoadImage(projectId, message.id)
      .then((value) => {
        if (active) setPreview(value);
      })
      .catch((error) => {
        if (active) {
          setFailure(error instanceof Error ? error.message : "图片不可用");
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [message.id, onLoadImage, projectId, visible]);

  return (
    <div className="wechat-message-image" ref={frameRef}>
      {preview
        ? <img src={preview.dataUrl} alt="会话图片" loading="lazy" />
        : failure
          ? <span className="wechat-image-fallback"><ImageOff size={16} />图片不可用：{failure}</span>
          : loading
            ? <span className="wechat-image-fallback"><LoaderCircle className="spin" size={16} />正在读取图片…</span>
            : <button type="button" className="wechat-image-fallback" onClick={() => setVisible(true)}>
                <ImageIcon size={16} />滚动到附近自动读取，或点击立即读取
              </button>}
    </div>
  );
}

import clsx from "clsx";
import { BookOpenText, CalendarClock, Info, MessageSquareText, Play, RefreshCw, Settings2 } from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import type { PageId } from "../types";

const navigation = [
  { id: "run" as const, label: "开始处理", icon: Play },
  { id: "schedules" as const, label: "定时导出", icon: CalendarClock },
  { id: "prompts" as const, label: "提示词", icon: MessageSquareText },
  { id: "settings" as const, label: "设置", icon: Settings2 },
];

export function Sidebar({
  page,
  onNavigate,
  version,
  onCheckForUpdates,
  checkingForUpdates,
  updateAvailable,
}: {
  page: PageId;
  onNavigate: (page: PageId) => void;
  version: string;
  onCheckForUpdates: () => void;
  checkingForUpdates: boolean;
  updateAvailable: boolean;
}) {
  const openDocumentation = async () => {
    try {
      await bridge.openDocumentation();
    } catch (error) {
      toast.error("无法打开使用说明", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
    }
  };

  return (
    <aside className="sidebar">
      <div className="sidebar-heading">工作台</div>
      <nav className="sidebar-nav">
        {navigation.map((item) => (
          <button
            key={item.id}
            className={clsx("nav-item", page === item.id && "nav-item-active")}
            onClick={() => onNavigate(item.id)}
          >
            <item.icon size={17} strokeWidth={2} />
            {item.label}
          </button>
        ))}
      </nav>
      <div className="sidebar-spacer" />
      <div className="sidebar-heading">帮助</div>
      <button className="nav-item" onClick={() => void openDocumentation()}><BookOpenText size={17} />使用说明</button>
      <button
        className={clsx("nav-item", updateAvailable && "nav-item-update")}
        onClick={onCheckForUpdates}
        disabled={checkingForUpdates}
        title={checkingForUpdates ? "正在检查更新" : "检查更新"}
      >
        <RefreshCw className={checkingForUpdates ? "spin" : undefined} size={17} />
        {checkingForUpdates ? "检查中…" : updateAvailable ? "有新版本" : "检查更新"}
        {updateAvailable && <span className="nav-update-dot" aria-label="有新版本" />}
      </button>
      <button
        className={clsx("nav-item", page === "about" && "nav-item-active")}
        onClick={() => onNavigate("about")}
      >
        <Info size={17} />关于
      </button>
      <div className="sidebar-version">
        <span className="status-dot" />本地运行 · v{version}
      </div>
    </aside>
  );
}

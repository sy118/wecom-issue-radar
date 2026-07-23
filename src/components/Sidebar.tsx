import clsx from "clsx";
import { BookOpenText, Info, MessageSquareText, Play, Settings2 } from "lucide-react";
import type { PageId } from "../types";

const navigation = [
  { id: "run" as const, label: "开始处理", icon: Play },
  { id: "prompts" as const, label: "提示词", icon: MessageSquareText },
  { id: "settings" as const, label: "设置", icon: Settings2 },
];

export function Sidebar({
  page,
  onNavigate,
  version,
}: {
  page: PageId;
  onNavigate: (page: PageId) => void;
  version: string;
}) {
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
      <button className="nav-item"><BookOpenText size={17} />使用说明</button>
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

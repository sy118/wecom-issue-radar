import { useEffect, useState } from "react";
import clsx from "clsx";
import { ChevronRight, Info, RefreshCw, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { getDesktop } from "../lib/desktop";
import { NAV_SECTIONS, defaultPageForSection, sectionForPage } from "../navigation";
import type { PageId } from "../types";

export function AppSidebar({
  page,
  version,
  onNavigate,
}: {
  page: PageId;
  version: string;
  onNavigate: (page: PageId) => void;
}) {
  const activeSection = sectionForPage(page);
  const [expanded, setExpanded] = useState<string>(activeSection.id);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    setExpanded(activeSection.id);
  }, [activeSection.id]);

  const openSection = (section: (typeof NAV_SECTIONS)[number]) => {
    setExpanded(section.id);
    onNavigate(defaultPageForSection(section));
  };

  const checkUpdates = async () => {
    setChecking(true);
    try {
      await getDesktop().invoke("updater_check");
      toast.success("已检查更新");
    } catch (error) {
      toast.error("检查更新失败", {
        description: error instanceof Error ? error.message : "请稍后重试。",
      });
    } finally {
      setChecking(false);
    }
  };

  return (
    <aside className="app-sidebar">
      <div className="sidebar-workspace">
        <span className="sidebar-workspace-label">本地工作区</span>
        <span className="sidebar-workspace-sub">
          <ShieldCheck size={13} /> 企微问题雷达 · 数据只在本机
        </span>
      </div>

      <nav className="sidebar-nav" aria-label="主导航">
        {NAV_SECTIONS.map((section) => {
          const Icon = section.icon;
          const isActive = section.id === activeSection.id;
          const isOpen = expanded === section.id;
          return (
            <div key={section.id} className={clsx("sidebar-group", isActive && "is-active")}>
              <button
                type="button"
                className={clsx("sidebar-section", isActive && "is-active")}
                aria-expanded={isOpen}
                onClick={() => (isOpen && section.pages.length ? setExpanded("") : openSection(section))}
              >
                <span className="sidebar-section-icon">
                  <Icon size={17} strokeWidth={1.9} />
                </span>
                <span className="sidebar-section-label">{section.label}</span>
                {section.pages.length > 0 && (
                  <ChevronRight className={clsx("sidebar-chevron", isOpen && "is-open")} size={15} />
                )}
              </button>
              {isOpen && section.pages.length > 0 && (
                <div className="sidebar-pages">
                  {section.pages.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      className={clsx("sidebar-page", page === item.id && "is-current")}
                      title={item.hint}
                      onClick={() => onNavigate(item.id)}
                    >
                      <span className="sidebar-page-dot" />
                      <span>{item.label}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </nav>

      <div className="sidebar-footer">
        <button type="button" className="sidebar-footer-button" onClick={() => void checkUpdates()} disabled={checking}>
          <RefreshCw size={14} className={checking ? "spin" : undefined} />
          {checking ? "检查更新中…" : `v${version} · 检查更新`}
        </button>
        <button type="button" className="sidebar-footer-button" onClick={() => onNavigate("about")}>
          <Info size={14} /> 关于与帮助
        </button>
      </div>
    </aside>
  );
}

import clsx from "clsx";
import { getDesktop } from "../lib/desktop";
import { NAV_SECTIONS, type NavSection, type SectionId } from "../navigation";

export function TopNav({
  section,
  onSelectSection,
  version,
}: {
  section: NavSection;
  onSelectSection: (section: NavSection) => void;
  version: string;
}) {
  const openUpdate = async () => {
    try {
      await getDesktop().invoke("updater_check");
    } catch {
      // The updater check is best-effort from the nav; the dialog owns errors.
    }
  };

  return (
    <div className="top-nav">
      <div className="top-nav-brand">
        <span className="top-nav-mark">W</span>
        <span className="top-nav-name">企微问题雷达</span>
      </div>
      <nav className="top-nav-sections" aria-label="主导航">
        {NAV_SECTIONS.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              className={clsx("top-nav-item", section.id === item.id && "top-nav-item-active")}
              onClick={() => onSelectSection(item)}
            >
              <Icon size={17} strokeWidth={2} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>
      <div className="top-nav-actions">
        <button type="button" className="top-nav-version" title="检查更新" onClick={() => void openUpdate()}>
          v{version}
        </button>
      </div>
    </div>
  );
}

export function SubNav({
  section,
  page,
  onNavigate,
}: {
  section: NavSection;
  page: string;
  onNavigate: (page: NavSection["pages"][number]["id"]) => void;
}) {
  if (section.pages.length === 0) return null;
  return (
    <aside className="sub-nav">
      <div className="sub-nav-heading">{section.label}</div>
      <nav className="sub-nav-list">
        {section.pages.map((item) => (
          <button
            key={item.id}
            type="button"
            className={clsx("sub-nav-item", page === item.id && "sub-nav-item-active")}
            onClick={() => onNavigate(item.id)}
          >
            <span className="sub-nav-label">{item.label}</span>
            {item.hint && <span className="sub-nav-hint">{item.hint}</span>}
          </button>
        ))}
      </nav>
    </aside>
  );
}

export type { SectionId };

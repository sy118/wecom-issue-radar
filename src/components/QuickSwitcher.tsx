import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import clsx from "clsx";
import { CornerDownLeft, Search } from "lucide-react";
import { NAV_SECTIONS, type NavSection } from "../navigation";
import type { PageId } from "../types";
import { Overlay } from "./Overlay";

interface QuickSwitcherItem {
  id: PageId;
  label: string;
  section: string;
  hint: string;
  icon: NavSection["icon"];
}

const WORKBENCH_ITEM: QuickSwitcherItem = {
  id: "workbench",
  label: "工作台",
  section: "工作台",
  hint: "今日概览、配置检查与快捷入口",
  icon: NAV_SECTIONS[0].icon,
};

const ITEMS: QuickSwitcherItem[] = [
  WORKBENCH_ITEM,
  ...NAV_SECTIONS.flatMap((section) =>
    section.pages.map((page) => ({
      id: page.id,
      label: page.label,
      section: section.label,
      hint: page.hint ?? "",
      icon: section.icon,
    })),
  ),
];

function matches(item: QuickSwitcherItem, query: string): boolean {
  if (!query) return true;
  const haystack = `${item.label} ${item.section} ${item.hint}`.toLowerCase();
  return haystack.includes(query);
}

/**
 * Keyboard-first page switcher (Ctrl/Cmd+K). Renders through the shared
 * Overlay primitive, so focus trapping, Esc and focus restore already behave
 * like every other dialog in the app.
 */
export function QuickSwitcher({
  open,
  onClose,
  onNavigate,
}: {
  open: boolean;
  onClose: () => void;
  onNavigate: (page: PageId) => void;
}) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const listRef = useRef<HTMLDivElement | null>(null);

  const items = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return ITEMS.filter((item) => matches(item, normalized));
  }, [query]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLElement>(`[data-index="${activeIndex}"]`);
    node?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  const choose = (item: QuickSwitcherItem | undefined) => {
    if (!item) return;
    onClose();
    onNavigate(item.id);
  };

  const onInputKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((current) => (items.length ? (current + 1) % items.length : 0));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((current) => (items.length ? (current - 1 + items.length) % items.length : 0));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      choose(items[activeIndex]);
    }
  };

  return (
    <Overlay
      open={open}
      onClose={onClose}
      size="sm"
      eyebrow="QUICK NAVIGATION"
      title="快速跳转"
      closeOnBackdrop
      ariaLabel="快速跳转"
    >
      <div className="quick-switcher" data-quick-switcher>
        <div className="quick-switcher-search">
          <Search size={17} aria-hidden="true" />
          <input
            data-autofocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onInputKeyDown}
            placeholder="搜索页面，例如：导出、监听、设置"
            aria-label="搜索页面"
            role="combobox"
            aria-expanded="true"
            aria-controls="quick-switcher-list"
            aria-autocomplete="list"
            aria-activedescendant={items[activeIndex] ? `quick-switcher-item-${items[activeIndex].id}` : undefined}
            autoComplete="off"
          />
          <kbd>Esc</kbd>
        </div>

        <div
          className="quick-switcher-list"
          id="quick-switcher-list"
          role="listbox"
          aria-label="页面列表"
          ref={listRef}
        >
          {items.map((item, index) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                type="button"
                id={`quick-switcher-item-${item.id}`}
                data-index={index}
                role="option"
                aria-selected={index === activeIndex}
                className={clsx("quick-switcher-item", index === activeIndex && "is-active")}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => choose(item)}
              >
                <span className="quick-switcher-icon">
                  <Icon size={16} strokeWidth={1.9} aria-hidden="true" />
                </span>
                <span className="quick-switcher-copy">
                  <strong>{item.label}</strong>
                  <small>
                    {item.section}
                    {item.hint ? ` · ${item.hint}` : ""}
                  </small>
                </span>
                {index === activeIndex && <CornerDownLeft className="quick-switcher-enter" size={14} aria-hidden="true" />}
              </button>
            );
          })}
          {items.length === 0 && (
            <p className="quick-switcher-empty">没有匹配的页面，试试「设置」或「导出」。</p>
          )}
        </div>

        <footer className="quick-switcher-foot">
          <span>
            <kbd>↑ ↓</kbd> 选择
          </span>
          <span>
            <kbd>Enter</kbd> 打开
          </span>
          <span>
            <kbd>Esc</kbd> 关闭
          </span>
        </footer>
      </div>
    </Overlay>
  );
}

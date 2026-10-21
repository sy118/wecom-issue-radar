import {
  Building2,
  CalendarClock,
  FileSearch,
  FileSpreadsheet,
  LayoutDashboard,
  MessageCircle,
  MessageSquareReply,
  Settings2,
  Sparkles,
  Wand2,
  type LucideIcon,
} from "lucide-react";
import type { PageId } from "./types";

export type SectionId = "workbench" | "wecom" | "wechat" | "daily" | "strategy" | "settings";

export interface NavPage {
  id: PageId;
  label: string;
  hint?: string;
}

export interface NavSection {
  id: SectionId;
  label: string;
  icon: LucideIcon;
  pages: NavPage[];
}

export const NAV_SECTIONS: NavSection[] = [
  {
    id: "workbench",
    label: "工作台",
    icon: LayoutDashboard,
    pages: [],
  },
  {
    id: "wecom",
    label: "企业微信",
    icon: Building2,
    pages: [
      { id: "wecom-issue", label: "问题雷达", hint: "整理群聊问题并导出" },
      { id: "wecom-schedules", label: "定时任务", hint: "每天自动跑" },
      { id: "wecom-reply", label: "群监听回复", hint: "群里的问题自动拟稿" },
    ],
  },
  {
    id: "wechat",
    label: "个人微信",
    icon: MessageCircle,
    pages: [
      { id: "wechat-workbench", label: "会话草稿", hint: "读取会话并生成草稿" },
      { id: "wechat-export", label: "导出会话", hint: "按项目和范围导出" },
    ],
  },
  {
    id: "daily",
    label: "日常助手",
    icon: Sparkles,
    pages: [
      { id: "daily-search", label: "聊天查找", hint: "跨群和会话全文搜索" },
      { id: "daily-reply", label: "高情商回复", hint: "三版措辞，只复制不发送" },
      { id: "daily-report", label: "周报总结", hint: "把一周消息整理成草稿" },
    ],
  },
  {
    id: "strategy",
    label: "策略与发布",
    icon: Wand2,
    pages: [
      { id: "strategy-prompts", label: "提示词库", hint: "AI 怎么理解与生成" },
      { id: "strategy-publish", label: "发布目标", hint: "Excel / Markdown / 腾讯文档" },
      { id: "strategy-mcp", label: "MCP 服务", hint: "外部工具接入（高级）" },
      { id: "strategy-dify", label: "Dify 接入", hint: "外部 AI 应用（高级）" },
    ],
  },
  {
    id: "settings",
    label: "设置",
    icon: Settings2,
    pages: [
      { id: "settings", label: "常用与高级" },
      { id: "about", label: "关于" },
    ],
  },
];

const SECTION_BY_PAGE = new Map<PageId, NavSection>();
for (const section of NAV_SECTIONS) {
  for (const page of section.pages) SECTION_BY_PAGE.set(page.id, section);
}

export function sectionForPage(page: PageId): NavSection {
  if (page === "workbench") return NAV_SECTIONS[0];
  const section = SECTION_BY_PAGE.get(page);
  if (section) return section;
  if (page === "about") return NAV_SECTIONS[NAV_SECTIONS.length - 1];
  return NAV_SECTIONS[0];
}

export function defaultPageForSection(section: NavSection): PageId {
  return section.pages[0]?.id ?? "workbench";
}

export function pageLabel(page: PageId): string {
  if (page === "workbench") return "工作台";
  for (const section of NAV_SECTIONS) {
    const match = section.pages.find((item) => item.id === page);
    if (match) return match.label;
  }
  return "工作台";
}

export const ICONS = {
  issue: FileSearch,
  schedule: CalendarClock,
  reply: MessageSquareReply,
  export: FileSpreadsheet,
};

export type { LucideIcon };

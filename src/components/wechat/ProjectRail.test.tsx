import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ProjectRail } from "./ProjectRail";
import type { WechatDetectResult, WechatProject } from "../../pages/wechat/types";

const project = {
  id: "p1",
  accountId: "wxid_me",
  chatId: "wxid_friend",
  chatTitle: "Friend A",
  name: "Project A",
  kind: "direct",
  enabled: true,
  prompt: "",
  summary: "",
  trigger: { mode: "manual" },
  revision: 1,
  updatedAt: "",
  draft: {
    content: "",
    status: "idle",
    stale: false,
    errorCode: "",
    errorMessage: "",
  },
} as unknown as WechatProject;

const detect = {
  found: true,
  running: true,
  dbDir: "E:/wechat/db_storage",
  accounts: [],
  candidates: [],
  selected: true,
  ready: true,
} as unknown as WechatDetectResult;

describe("ProjectRail", () => {
  it("renders a quick delete action for every local project", () => {
    const html = renderToStaticMarkup(
      <ProjectRail
        projects={[project]}
        selectedId={project.id}
        detect={detect}
        detectLoading={false}
        backendAvailable
        loading={false}
        error=""
        unreadFor={() => 0}
        onSelect={() => undefined}
        onDelete={() => undefined}
        onCreate={() => undefined}
        onRefresh={() => undefined}
        onExport={() => undefined}
        onOpenConfig={() => undefined}
      />,
    );

    expect(html).toContain("wechat-project-delete");
    expect(html).toContain("Project A");
  });
});

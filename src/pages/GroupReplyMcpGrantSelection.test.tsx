import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { buildListenerSaveBody, defaultListenerDraft } from "../lib/replyRuntimeUi";

function source(file: string): string {
  return readFileSync(fileURLToPath(new URL(file, import.meta.url)), "utf8");
}

describe("group reply P1 listener policy", () => {
  it("saves Agent V2 listeners without per-tool grants", () => {
    const body = buildListenerSaveBody({
      draft: {
        ...defaultListenerDraft(),
        id: "listener-1",
        name: "售后群答疑",
        enabled: true,
        groupId: "R:after-sales",
        groupName: "售后群",
      },
      webhookEdit: { mode: "keep" },
    });

    expect(body.listener).toMatchObject({
      id: "listener-1",
      runtimeVersion: "v2",
      answerEngine: "mcp",
    });
    expect(body.listener).not.toHaveProperty("toolGrants");
  });

  it("has a four-step editor without runtime selection or tool-grant controls", () => {
    const drawer = source("./groupreply/ListenerEditorDrawer.tsx");

    expect(drawer).not.toContain("Legacy");
    expect(drawer).not.toContain("runtime-version-choice");
    expect(drawer).not.toContain("tool-grant-list");
    expect(drawer).not.toContain("selectedTools");
    expect(drawer).not.toContain("toolStrategies");

    for (const step of ["监听对象", "引擎与 Runtime", "发送方式", "预算"]) {
      expect(drawer).toContain(step);
    }
    expect(drawer.match(/<a href="#listener-/g)).toHaveLength(4);
  });

  it("keeps the listener card free of legacy badges while retaining native tool stats", () => {
    const list = source("./groupreply/ListenerList.tsx");

    expect(list).not.toContain("LEGACY");
    expect(list).toContain("AGENT V2");
    expect(list).toContain("listener-fact-value");
    expect(list).toContain("精确授权工具");
    expect(list).toContain("只读 / 已阻止");
    expect(list).toContain("轮次 / 调用预算");
  });
});

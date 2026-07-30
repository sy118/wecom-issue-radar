import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  isMcpToolGrantable,
  mcpServerGrantState,
  toggleMcpServerGrant,
  toolGrantSelectionKey,
} from "../lib/replyRuntimeUi";

describe("group reply MCP service grants", () => {
  it("only grants tools whose current schema fingerprint is confirmed", () => {
    expect(isMcpToolGrantable({ schemaStatus: "current", schemaSha256: "sha-current" })).toBe(true);
    expect(isMcpToolGrantable({ schemaStatus: "current", schemaSha256: "" })).toBe(false);
    expect(isMcpToolGrantable({ schemaStatus: "changed", schemaSha256: "sha-changed" })).toBe(false);
    expect(isMcpToolGrantable({ schemaStatus: "unknown", schemaSha256: "sha-unknown" })).toBe(false);
  });

  it("selects a whole service by default, then preserves exact per-tool overrides", () => {
    const search = toolGrantSelectionKey("knowledge", "search", "sha-search");
    const article = toolGrantSelectionKey("knowledge", "get_article", "sha-article");
    const unrelated = toolGrantSelectionKey("crm", "lookup_customer", "sha-crm");
    const serviceTools = [search, article];

    const selectedFromNone = toggleMcpServerGrant([unrelated], serviceTools);
    expect(mcpServerGrantState(selectedFromNone, serviceTools)).toBe("all");
    expect(new Set(selectedFromNone)).toEqual(new Set([unrelated, search, article]));

    const partial = [unrelated, search];
    expect(mcpServerGrantState(partial, serviceTools)).toBe("partial");
    const completed = toggleMcpServerGrant(partial, serviceTools);
    expect(mcpServerGrantState(completed, serviceTools)).toBe("all");
    expect(new Set(completed)).toEqual(new Set([unrelated, search, article]));

    const cleared = toggleMcpServerGrant(completed, serviceTools);
    expect(mcpServerGrantState(cleared, serviceTools)).toBe("none");
    expect(cleared).toEqual([unrelated]);
  });

  it("treats an empty or duplicated catalog as a stable set of grants", () => {
    const search = toolGrantSelectionKey("knowledge", "search", "sha-search");
    const article = toolGrantSelectionKey("knowledge", "get_article", "sha-article");
    const unrelated = toolGrantSelectionKey("crm", "lookup_customer", "sha-crm");

    expect(mcpServerGrantState([search], [])).toBe("none");
    expect(toggleMcpServerGrant([unrelated, unrelated], [])).toEqual([unrelated]);

    const selected = toggleMcpServerGrant(
      [unrelated, unrelated, search],
      [search, search, article],
    );
    expect(selected).toEqual([unrelated, search, article]);
    expect(mcpServerGrantState(selected, [search, search, article])).toBe("all");
  });

  it("renders services collapsed with a whole-service default and exact expanded controls", () => {
    const source = readFileSync(
      fileURLToPath(new URL("./GroupReplyPage.tsx", import.meta.url)),
      "utf8",
    );

    expect(source).toContain("selectedTools: toggleMcpServerGrant([], defaultServerToolKeys)");
    expect(source).toContain("toolGroups.length ? toolGroups.map");
    expect(source).toContain("mcpServerGrantState(draft.selectedTools, grantableKeys)");
    expect(source).toContain("aria-expanded={expanded}");
    expect(source).toContain("expanded && <div className=\"mcp-grant-tools\">");
    expect(source).toContain("toggleToolSelection(draft.selectedTools, tool.key)");
  });
});

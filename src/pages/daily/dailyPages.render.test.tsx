import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DailyDigestPage } from "./DailyDigestPage";
import { DailyPhrasesPage } from "./DailyPhrasesPage";
import { DailyReplyPage } from "./DailyReplyPage";

/**
 * Static render smoke tests: the repo has no jsdom/RTL, but `react-dom/server`
 * still executes the components, so a broken import or JSX branch fails here
 * instead of only in the packaged app. Effects (bridge loads) do not run, which
 * is exactly what we want for the initial, local-only shell.
 */

describe("daily pages render their initial shell", () => {
  it("renders the reply assistant inputs and safety note", () => {
    const html = renderToStaticMarkup(<DailyReplyPage />);
    expect(html).toContain("回复助手");
    expect(html).toContain("对方的消息");
    expect(html).toContain("解读潜台词并生成三版");
    expect(html).toContain("不会点击发送");
  });

  it("renders the phrase library toolbar before any data arrives", () => {
    const html = renderToStaticMarkup(<DailyPhrasesPage />);
    expect(html).toContain("话术库");
    expect(html).toContain("新建话术");
    expect(html).toContain("正在读取话术");
    expect(html).toContain("{{姓名}}");
  });

  it("renders both digest modes and the export affordance only after results", () => {
    const html = renderToStaticMarkup(<DailyDigestPage />);
    expect(html).toContain("长文速读");
    expect(html).toContain("会议纪要");
    expect(html).toContain("开始速读");
    // No result yet: the editable result panel must not be mounted.
    expect(html).not.toContain("导出 Markdown");
  });
});

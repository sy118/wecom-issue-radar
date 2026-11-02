# Tasks

## 1. 规划

- [x] 1.1 完成 proposal / specs / design / tasks，并通过 `openspec-cn validate wecom-high-eq-reply`。

## 2. 后端上下文读取

- [x] 2.1 在 `worker/reply_runtime/message_source.py` 增加 `read_recent_conversations(limit)` 与 `read_recent_context(conversationId, limit, windowSeconds)`（只读、不推进游标、复用身份缓存与图片描述符），并补单测验证窗口/排序/字段与图片占位。验证：`python -m unittest tests.test_reply_assistant` 相关用例通过。
- [x] 2.2 在 `worker/reply_runtime/runtime.py` 的 query 分发新增 `chat.recent` / `chat.context`，参数校验与协议错误信息完整；补运行时查询测试（伪造消息源或快照）。验证：`python -m unittest discover -s tests` 通过。
- [x] 2.3 在 `src/types.ts` 增加 `RecentChat` / `ChatContextMessage` / `ChatOcrResult` 等前端类型；`scripts/ui-preview.mjs` 的 mock 运行时补对应 query 结果。验证：`pnpm typecheck` 通过。

## 3. 图片 OCR

- [x] 3.1 重构 `worker/ocr.py`：抽出 `resolve_ocr_engine(config)`（独立 OCR 优先，回退全局模型并区分 OpenAI/Anthropic 图片负载）与 `ocr_image_file(...)`，保留现有 `run_ocr` 行为；补单测覆盖引擎选择、缓存键、失败降级。验证：`python -m unittest tests.test_reply_assistant` 通过。
- [x] 3.2 在 runtime 新增 `chat.ocr` 查询：按 conversationId+messageIds 重新解析图片路径、并发 2、单图 60s、总 45s、每轮 ≤6 张，结果写入 `<dataRoot>/cache/chat-ocr/*.json` 并复用缓存。验证：单测覆盖上限、超时与缓存命中。
- [x] 3.3 前端 OCR 展示：识别文本以可展开折叠块出现在图片消息中，未识别/超时占位与设置引导。验证：`highEqReply.logic.test.ts` 覆盖合并逻辑，`pnpm typecheck`/`pnpm test` 通过。

## 4. 回复助手引擎迁移与 IPC

- [x] 4.1 新建 `worker/reply_assistant/`（actions/prompts/rules/model），只保留 eq_reply；系统提示词优先读取 `config.prompts.items` 中 `scene == "eq_reply"` 的内容，缺省回退内置提示词；删除 `worker/daily/` 与 `tests/test_daily_actions.py`，迁移等价测试为 `tests/test_reply_assistant.py`。验证：Python 测试通过。
- [x] 4.2 更新 `worker/main.py` 分发与 `worker/issue_radar_worker.spec` 隐藏导入；IPC `daily_action` 改为 `assistant_action`（contracts/ipc/services/bridge 全链路），移除 summarize/phrases 动作白名单。验证：`pnpm typecheck` 与 `python -m unittest discover -s tests` 通过。

## 5. 前端「高情商回复」页面

- [x] 5.1 新增 `src/pages/wecom/HighEqReplyPage.tsx`、`highEqReply.logic.ts`、`highEqReply.css`：会话列表（群聊/私聊/默认最近/切换/手动刷新）、上下文（30 条/6 小时、待回复对象高亮可切换）、生成结果（解读/三版/检查/复制）、空态与错误引导。验证：`pnpm typecheck` 与新增 `highEqReply.logic.test.ts` 通过。
- [x] 5.2 轮询与 OCR 编排：页面可见时 30s 轮询会话列表与上下文；上下文变化后对新增图片触发 `chat.ocr`（≤6 张）；页面不可见停止轮询；生成中/复制失败/读取失败状态齐全。验证：逻辑单测覆盖轮询去重与不可见暂停，`pnpm test` 通过。

## 6. 删除沟通助手

- [x] 6.1 删除 `src/pages/daily/**`、`src/components/daily/**`、`daily.css` 引用与相关测试；更新 `src/navigation.ts`（移除 daily 分组、新增 `wecom-high-eq-reply` 页面）、`src/App.tsx`、`src/types.ts`、`src/pages/WorkbenchPage.tsx`（快捷入口改为高情商回复）、`src/pages/PromptsPage.tsx`（eq_reply 场景改名高情商回复）。验证：`git grep` 全仓无 `daily-reply|daily-phrases|daily-digest|沟通助手|话术库|长文速读` 残留（历史 CHANGELOG 除外），`pnpm test` 全绿。
- [x] 6.2 清理 IPC/服务/桥接中 `daily_action`、`dailyAction` 与 `phrases.json` 相关代码；确认 `phrases.json` 文件不被删除、不被读取。验证：`pnpm typecheck`、Python 测试通过。

## 7. 文档与截图

- [x] 7.1 更新 `README.md`（能力地图、5 分钟上手、上下文与 OCR 边界、FAQ、快捷键）、`docs/electron-migration.md`（v4.6 变更与限制）、`docs/README.md` 与 `docs/release.md` 版本引用、`CHANGELOG.md` v4.6.0（含 BREAKING 说明）。验证：链接可达、版本号与 `package.json` 一致。
- [x] 7.2 `scripts/ui-preview.mjs` 删除 daily 路由、新增 `wecom-high-eq-reply` 路由与 mock（会话/上下文/图片 OCR 态），生成浅色/深色/1024 截图，更新 `docs/images/` 中相关图片与 README 预览表。验证：截图无溢出、无旧模型名、可访问性报告无未命名按钮。

## 8. 验证与发布

- [x] 8.1 本地门禁：`pnpm typecheck`、`pnpm test`、`pnpm build:renderer`、`python -m unittest discover -s tests` 全部通过（不执行 PyInstaller/NSIS）。
- [x] 8.2 `package.json` 升到 `4.6.0`，提交并推送 `main`；`gh run list` 确认 CI 与 Build Windows installers 成功、Artifact 可下载。
- [ ] 8.3 推送 tag `v4.6.0`，用 `gh run watch` / `gh release view` 验证 NSIS 安装包、`latest.yml`、blockmap 与兼容 `latest.json` 已发布。

## Workflow follow-up

- 发布验证完成后归档该变更。

# Tasks

## 1. 规划

- [x] 1.1 创建 OpenSpec 变更，完成 proposal / specs / design / tasks 并通过 `openspec-cn validate settings-advanced-shell-and-reply-note`。

## 2. 高级设置保存

- [x] 2.1 `SettingsPage` 支持嵌入模式：渲染紧凑工具栏（标题、说明、保存状态、保存按钮），保留现有校验与 `onSave`，删除 `nav.css` 中隐藏 `page-title-row` 的规则。
- [x] 2.2 dirty 状态贯穿顶部与吸底保存条；`onDirtyChange` 通知 `SettingsHome`，常用/高级切换与离开页面前按需确认；补测试。
- [x] 2.3 长表单吸底保存条：共享 saving/disabled，窄窗口与滚动场景可见；补 1024 视觉验收。

## 3. 高级页样式

- [x] 3.1 统一高级页壳层：tabs segmented、卡片 16px 间距、两列字段网格、单一滚动容器；配置备份与迁移独立成块。
- [x] 3.2 深色/1024×700 截图与溢出检查，更新 `scripts/ui-preview.mjs` 或临时预览（不改变生产行为）。

## 4. 高情商回复补充要求

- [x] 4.1 页面在待回复消息下增加「本次补充要求（可选）」输入（≤500 字、字数、清空），切换会话清空、重新生成保留；补前端测试。
- [x] 4.2 `eqReplyPayload` 增加 `replyInstruction`（trim/500 字截断，空值不带），生成调用传入；补 logic 测试。
- [x] 4.3 后端 `actions.py` 接收 `replyInstruction`，`prompts.py` 在「需要回复的消息」之后、称呼/上下文之前注入「本次回复补充要求」；空值不改变 prompt；补单测。

## 5. 验证与文档

- [x] 5.1 本地门禁：`pnpm typecheck`、`pnpm test`、`pnpm build:renderer`、`python -m unittest discover -s tests`。
- [x] 5.2 README / docs 更新高级保存入口与本次补充要求；浅色/深色/1024 截图更新。

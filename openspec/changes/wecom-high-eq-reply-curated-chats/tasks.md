# Tasks

## 1. 规划

- [x] 1.1 创建 OpenSpec 变更，完成 proposal / specs / design / tasks 并通过 `openspec-cn validate wecom-high-eq-reply-curated-chats`。

## 2. 后端：会话清单存储与命令

- [x] 2.1 `worker/reply_runtime/store.py` 增加 `reply_assistant_chats` 表（CREATE IF NOT EXISTS，无需 ALTER 迁移）及基础查询助手。验证：迁移在旧库上可重复执行。
- [x] 2.2 `worker/reply_runtime/runtime.py` 增加 `chat.chats.add` / `chat.chats.rename` / `chat.chats.remove` 写命令：参数校验、去重、别名 trim、revision 返回、未知会话处理；沿用现有事务/幂等机制。验证：单测覆盖添加、重复添加改别名、改名、清空别名、移除、非法 ID/revision 冲突。

## 3. 后端：目录查询与 curated recent

- [x] 3.1 `chat.catalog` 查询：`{limit?:300, search?}`，复用运行时目录缓存返回全部会话（含 alias/added），按最后消息时间降序；非法/超限 clamp。验证：搜索、limit、别名/已添加标记。
- [x] 3.2 `_query_chat_recent` 改为只返回已添加会话：合并清单表 + 目录缓存，返回 `{chats, revision, selfSenderId, selfSenderName}`；缺失会话 `available:false` 且保留别名；空清单返回空数组。验证：只含已添加、排序、缺失会话、revision 字段。

## 4. 后端：多消息回复 prompt

- [x] 4.1 `worker/reply_assistant/actions.py` + `prompts.py`：`eq_reply` 支持可选 `focusMessages`（最多 20 条），生成「需要回复的消息（共 N 条）」列表并提示综合回复；`text` 为空但有 focusMessages 时仍可生成；规则回退使用拼接文本；无 text / focusMessages 仍报错。验证：单测覆盖单条兼容、多条 prompt、空输入报错、规则回退。

## 5. 前端：我的会话列表

- [x] 5.1 左侧改为已添加会话清单：空态引导「添加会话」；添加弹窗（`chat.catalog` 搜索、多选、已添加禁用）调用 `chat.chats.add`；重命名（本地别名）与移除（确认）调用对应命令；命令使用 `chat.recent` 返回的 revision，冲突时刷新重试。
- [x] 5.2 轮询/上下文只在已添加且有选中会话时进行；别名优先显示并同步到上下文标题；添加/移除后保持合理选中项（删除当前会话时选中下一条或置空）。验证：前端逻辑单测 + 预览。

## 6. 前端：多选回复

- [x] 6.1 选择模型：`selected` 消息快照；私聊默认最后一条、群聊默认空；点击消息 toggle，目标面板列出选中项（移除/清空），选中项在上下文中高亮。
- [x] 6.2 生成 payload：`focusMessages`、拼接 `text`、context 排除选中消息、图片并集（≤6 张）；0 选中时结合上下文生成；手动输入模式忽略选中；`canGenerate` 与提示文案同步。验证：vitest 覆盖 toggle/默认/0 选中/快照/图片并集/payload。

## 7. 文档与截图

- [x] 7.1 README / docs 更新：会话清单添加/改名/移除、只同步已添加、群聊默认不选与多选综合回复、私聊默认最后一条；CHANGELOG v4.6.5。验证：相对链接、无旧口径残留。
- [x] 7.2 `scripts/ui-preview.mjs` mock 补 `chat.catalog`、清单命令、多选状态；重拍浅/深/1024 截图并更新 `docs/images/`。

## 8. 验证与发布

- [x] 8.1 本地门禁：`pnpm typecheck`、`pnpm test`、`pnpm build:renderer`、`python -m unittest discover -s tests`。
- [ ] 8.2 `package.json` 升到 `4.6.5`，推送 `main`，确认 CI 与 Windows Artifact 成功。
- [ ] 8.3 推送 tag `v4.6.5`，验证 Release 资产（setup.exe/latest.yml/blockmap/latest.json）并归档该变更。

## Workflow follow-up

- 发布验证完成后归档该变更。

# Tasks

## 1. 规划

- [x] 1.1 创建 OpenSpec 变更，完成 proposal / specs / design / tasks 并通过 `openspec-cn validate wecom-high-eq-reply-context-ux`。

## 2. 后端：会话名称与目录缓存

- [x] 2.1 `worker/wecom/local_db.py`：新增直聊自己身份识别（`user_table.account`/`id` 匹配会话前缀）、`load_conversation_directory`（一次 session.db 读取会话行 + 成员昵称）、`load_user_directory`（一次 user.db 读取用户映射 + 账号），并实现「对方优先 + 过滤自己名字 + 重复标题兜底 + 私聊会话」取名。验证：单测覆盖四种取名路径。
- [x] 2.2 `worker/reply_runtime/message_source.py`：加入目录缓存（TTL 30s，`refresh` 绕过）、上下文缓存（TTL 10s）、预热线程 `warmup()`；`recent_conversations(refresh=False)` 使用缓存并返回 `selfSenderId/selfSenderName`；`read_context` 使用目录缓存取名、不再二次解密 `session.db`。验证：Python 单测覆盖缓存命中/过期/refresh 与预热静默失败。

## 3. 后端：分页与运行时查询参数

- [x] 3.1 `message_source.read_context` 支持 `before` 与 `hasMore`：无 before 时取最近 `limit` 条（6h + 30d 回退），有 before 时取 `sendTime <= before` 的最近 `limit` 条（30d 窗口），多取 1 条计算 `hasMore`。验证：单测覆盖首页、更早页、重复边界、hasMore。
- [x] 3.2 `runtime.py`：`chat.recent` 支持 `refresh` 并返回 `selfSenderId/selfSenderName`；`chat.context` 支持 `limit/before/refresh`，透传 `hasMore/windowFallback/selfSenderId`。验证：运行时查询单测与旧参数兼容。

## 4. 前端：固定视口与滚动交互

- [x] 4.1 `highEqReply.css` / `HighEqReplyPage.tsx`：工作区固定高度（≥1280）、三栏内部滚动、上下文流 `flex:1; min-height:0; overflow-y:auto`；≤1279 / ≤900 回退自动高度；页面不再整体滚动；长消息仅超长时折叠。验证：1024×700 / 1280×820 UI 预览无横向溢出、默认可见最新消息。
- [x] 4.2 滚动行为：进入/切换会话默认滚到底部；`stickToBottom` 跟随新消息；向上滚后显示「跳到最新」；更新 30s 轮询时不打断用户阅读位置。验证：前端逻辑单测 + 预览手动滚动截图。

## 5. 前端：分页、缓存与刷新让路

- [x] 5.1 `highEqReply.logic.ts`：新增分页合并（按 messageId 去重、保持升序、计算新增高度）、会话缓存类型与 `CONTEXT_PAGE_SIZE=20`、`shouldLoadOlder`/`stickToBottom` 判定、名称回退。验证：vitest 覆盖合并/去重/滚动位置/名称回退。
- [x] 5.2 `HighEqReplyPage.tsx`：按会话缓存上下文（命中先渲染再静默刷新）、滚动到顶加载更早页、手动刷新 `refresh=true`、点击会话时忽略在途轮询结果；请求参数携带 `before`/`limit`/`refresh`。验证：`pnpm typecheck` + vitest，预览中切换会话无明显白屏。

## 6. 文档

- [x] 6.1 README / docs 更新：上下文滚动视口与向上分页、默认最新、左侧会话名称规则（私聊显示对方、群聊显示群名、不显示自己）、缓存/预热的行为与「手动刷新可强制最新」；CHANGELOG v4.6.4。验证：相对链接可达、无旧口径残留。

## 7. 验证与发布

- [x] 7.1 本地门禁：`pnpm typecheck`、`pnpm test`、`pnpm build:renderer`、`python -m unittest discover -s tests` 全部通过；UI 预览补截图（浅/深/1024）。
- [ ] 7.2 `package.json` 升到 `4.6.4`，推送 `main`，确认 CI 与 Windows 构建成功、Artifact 可下载。
- [ ] 7.3 推送 tag `v4.6.4`，验证 Release 资产（NSIS 安装包、`latest.yml`、blockmap、`latest.json`）并归档该变更。

## Workflow follow-up

- 发布验证完成后归档该变更。

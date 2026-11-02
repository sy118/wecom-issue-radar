# Design

## Context

- 现有「沟通助手」三页：`src/pages/daily/*`（回复助手/话术库/长文速读），后端 `worker/daily/*`（eq_reply/summarize/phrases），IPC `daily_action`；回复助手要求用户手动粘贴消息。
- 企微读取链路已具备：`worker/wecom/local_db.py` 提供 `list_conversations`（含最后消息时间）、`read_messages`、`format_message`、`load_user_identities`；`worker/wecom/media.py` 可在只读范围内解析 Cache/Image 图片路径；常驻 `worker/reply_runtime` 已持有解密快照与身份缓存，并被 `electron/main/services/replyRuntime.ts` 监督，群监听页通过 `reply_runtime_query` 查询。
- OCR 已具备：`worker/ocr.py` 使用 `config.ocr`（Anthropic 兼容）逐图识别，按 `local_path` 缓存；`config.llm` 为全局模型（可能是 OpenAI-compatible 或 Anthropic）。
- 提示词库：`config.prompts.items` 已有 `scene=eq_reply` 场景（默认提示词），但当前 `worker/daily` 使用的是硬编码提示词，配置场景实际未生效。

## Goals / Non-Goals

**Goals:** 企业微信内自动上下文 + 图片 OCR 的高情商回复；删除沟通助手；提示词可编辑；仅复制；低延迟轮询。

**Non-Goals:** 不自动发送、不填企微输入框、不做企微窗口 UI 自动化识别"当前打开会话"、不改个人微信/群监听现有行为、不做 OCR 结果人工校正。

## Decisions

### D1 读取放在常驻回复运行时（新增查询 kind）

`reply_runtime_query` 新增 `chat.recent` / `chat.context` / `chat.ocr`，由 `ReplyRuntime` 持有 `config_path` 与 `LocalWeComMessageSource` 的快照能力。备选：新增一次性 worker action（每次轮询 spawn sidecar，打包版启动开销大、30s 轮询不可接受），不采用。

- `chat.recent`: 返回 `list_conversations(include_direct=True)` 结果（conversationId、displayName、lastMessageTime、是否群聊）。
- `chat.context`: `{conversationId, limit=30, windowSeconds=21600}` → 消息列表（messageId、senderId、senderName、sendTime、contentType、text、images 描述符、imageMd5Refs）。
- `chat.ocr`: `{conversationId, messageIds:[…]}` → 运行时按自身快照重新定位图片本地路径（不信任渲染层路径参数），返回每张图的识别文本/模型/错误。

### D2 上下文消息读取

在 `worker/reply_runtime/message_source.py` 增加 `read_recent(conversation_id, *, limit, window_seconds)`：`read_messages` + `format_message` + 身份缓存 + `_resolve_message_db_image_paths`，不推进监听游标、不写任何状态；图片只解析为本地描述符，OCR 在独立查询中进行。

### D3 OCR：独立模型优先 + 全局视觉模型回退 + 缓存与预算

`worker/ocr.py` 抽出可复用的单图能力：

- `resolve_ocr_engine(config)`: 若 `config.ocr` 配齐 base_url/api_key/model → `source="ocr"`；否则若 `config.llm` 可用 → 复用全局模型（按 `llm.provider` 走 OpenAI `image_url` 或 Anthropic `image` 内容块），`source="llm"`；都不可用抛可读错误。
- `ocr_image_file(path, *, engine, timeout=60)`：返回 `{text, model, source, error}`。
- 缓存：`<dataRoot>/cache/chat-ocr/<sha256(fileBytes+endpoint+model)>.json`，含压缩后的文本；只保留窗口内图片，避免重复计费。
- 预算：单轮 ≤6 张、单图 60s、总预算 45s、并发 2；超时/失败返回占位文本 `[图片 OCR 超时]` / `[图片 OCR 失败]`，不阻塞生成。

### D4 回复助手引擎迁移与提示词

`worker/daily` 的 eq 部分迁移为 `worker/reply_assistant/`（actions/prompts/rules/model），删除 summarize/phrases；IPC `daily_action` → `assistant_action`（仅 `eq_reply`），同时移除 `summarize`/`phrases_*` 白名单与前端 API。引擎从 `config.prompts.items` 中取第一条 `scene == "eq_reply"` 的 `content` 作为系统提示词（拼接既有 JSON 输出契约），找不到时用内置默认提示词。

### D5 前端页面

新增 `src/pages/wecom/HighEqReplyPage.tsx` + `highEqReply.logic.ts` + `highEqReply.css`：左侧会话列表（群聊/私聊，按时间排序，默认最近），中间上下文（30/6h，待回复对象高亮、可点选），右侧结果（解读/三版/检查/复制）。页面挂载且 `document.visibilityState === "visible"` 时每 30s 轮询 `chat.recent` + `chat.context`；上下文更新后对新增图片调用 `chat.ocr`（每轮 ≤6 张）。生成调用 `assistantAction("eq_reply", {text, context, recipient})`，`context` 在前端按 `[时间] 发送者: 内容` 合成（含 OCR 文本）。复制统一用 `src/lib/clipboard.ts`。所有错误/空态提供到设置页的引导。

### D6 删除与导航

删除 `src/pages/daily/**`、`src/components/daily/**`、`worker/daily/**`、`tests/test_daily_actions.py`、`daily_action` IPC/服务/桥接、导航 daily 分组、PageId daily-*、工作台快捷入口；提示词库场景 `eq_reply` 标签改为「高情商回复」并继续保留。`phrases.json` 不删除、不读取。

### D7 测试与验收

- Python：新增 `tests/test_reply_assistant.py`（提示词选择、JSON 回退、上下文窗口/字段、OCR 缓存键与预算）；`tests/test_daily_actions.py` 删除并迁移等价用例。
- 前端：`highEqReply.logic.test.ts`（排序/默认选中/上下文合成/OCR 合并/轮询去重）；更新 navigation/App 相关测试；`pnpm typecheck` / `pnpm test` / `pnpm build:renderer`。
- 视觉：`scripts/ui-preview.mjs` 删除 daily 路由、新增 `wecom-high-eq-reply` 路由与 mock（会话/上下文/OCR），出浅色/深色截图。
- 发布：版本 `4.6.0`，push main 出 Artifact，tag `v4.6.0` 自动发布 Release。

## Risks / Trade-offs

- [runtime.py 体积大，修改风险] → 新增查询只做薄入口，业务逻辑放独立模块/消息源方法，并补测试。
- [企微图片可能未缓存到本地或已被清理] → 描述符带 `available/error`，UI 显示占位且不阻塞生成。
- [OCR 费用与延迟] → 仅窗口内图片、缓存、6 张/45s 预算、失败降级；识别在后台进行，生成不强制等待。
- [30s 轮询频繁读库] → 复用常驻快照增量读取，页面不可见即停止。
- [删除板块引发引用残留] → 全仓 grep `daily`/`沟通助手`/`话术库`/`长文速读` 清理，测试与 CI 验证。
- [私聊/群聊 id 格式差异] → 统一走 `list_conversations(include_direct=True)`，UI 显示会话类型，读取用同一 `read_messages` 接口。

## Migration Plan

- 无配置 schema 变更；`phrases.json` 原样保留，仅停止读取。
- `daily_action` 为内部 IPC，无外部兼容承诺；发布说明标注 BREAKING（沟通助手移除）。
- 回滚：恢复上一个 tag（`v4.5.1`）或 revert 该提交；OCR 缓存目录可安全删除。

## Open Questions

（无）

# Design

## Context

高情商回复页（`src/pages/wecom/HighEqReplyPage.tsx`）当前三栏布局各自限制高度：会话列 `max-height: min(56vh,560px)`、上下文列 `max-height: min(60vh,620px)`，页面本身还会滚动；上下文一次渲染最近 30 条，每条最多 3 行截断。后端 `chat.recent` / `chat.context` 由常驻回复运行时串行处理，`list_conversations`、`load_user_map`、`load_member_names` 每次都重新解密数据库，`read_context` 还会额外解密一次 `session.db`；首次读取消息快照需要整库解密。直聊名称取自企微 `conversation_table` 标题，可能等于当前账号自己的名字。

## Goals / Non-Goals

**Goals：** 固定高度滚动视口、默认最新、向上分页；直聊显示对方、群聊显示群名且永不显示自己；缓存 + 预热让首次打开与切换会话都明显变快。

**Non-Goals：** 不改图片处理（仍为生成时交模型、加载不 OCR）；不在上下文里给消息加「我」标记（用户要求不做）；不改生成/复制/提示词库；不改变消息表只读原则；不做跨进程持久缓存。

## Decisions

### D1 上下文固定视口与滚动

- `.heq-workspace` 在 ≥1280 视口使用 `height: clamp(480px, calc(100vh - 190px), 920px)`，三栏 `align-items: stretch`；`≤1279` 时回退为两栏 + 自动高度（回复面板下移），`≤900` 单栏；窄布局下页面可滚动，但上下文面板内部仍保留滚动。
- 上下文面板：`display:flex; flex-direction:column; overflow:hidden`，header 固定，`.heq-context-body` `flex:1; min-height:0`，消息流 `flex:1; min-height:0; overflow-y:auto`。会话列改为内部滚动；回复列 `overflow-y:auto`。
- 默认停在底部：记录 `stickToBottomRef`（用户距底部 < 40px 视为跟随）；加载/切换后若 stick 则 `scrollTop = scrollHeight`，用户向上滚后新消息不抢滚动，显示「跳到最新」按钮。
- 长消息不再统一 3 行截断：仅当文本 > 600 字符或 > 12 行时保留「展开全文/收起」。

### D2 分页契约

`chat.context` 新增可选参数与返回字段（向后兼容）：

```
request  { kind:"chat.context", conversationId, limit?:20, windowSeconds?:21600,
           before?: number /* sendTime 秒，加载更早一页 */, refresh?: boolean }
response { conversationId, displayName, isGroup, windowFallback, hasMore,
           selfSenderId, messages:[...升序...] }
```

- 无 `before`：读取 6 小时窗口内最近 `limit` 条；若为空回退最近 30 天（`windowFallback=true`）。
- 有 `before`：读取 `sendTime <= before` 的最近 `limit` 条，`windowSeconds` 扩展为 30 天窗口。
- `hasMore`：后端多取 1 条判断是否还有更早消息；前端按 `messageId` 去重并保持 `sendTime` 升序。
- 前端在滚动到顶部（`scrollTop <= 24`）且 `hasMore` 时加载上一页，记录加载前 `scrollHeight`，插入后 `scrollTop += 新增高度`，避免跳动。

### D3 名称解析（后端）

`worker/wecom/local_db.py` 新增纯辅助：

- `direct_self_id` / `direct_peer_id`：拆 `S:<selfUserid>_<peerLocalId>`。
- 自己的身份：在 `user_table` 中按 `account == selfUserid`（其次 `id == int(selfUserid)`）找到本地用户 id 与姓名；`user_map` 同时返回 `account -> id` 映射。
- 直聊取名顺序：**对方名称**（`user_map[peer]` → 成员昵称）→ 企微标题（仅当其不等于自己的名字）→ 重复标题兜底（同一直聊标题在 ≥3 个会话出现视为自己的名字并丢弃）→ `私聊会话`。群聊保持 `roomname_remark or name`。
- `list_conversations` 与 `get_conversation_state` 复用上述解析；直聊不再回退显示原始会话 ID 或自己的名字。

### D4 运行时缓存与预热

`worker/reply_runtime/message_source.py`：

- `_directory_cache`：`{at, rows, user_map, member_names, self_id, self_name}`，TTL 30 秒；通过一次 `session.db` 解密读取会话行 + 成员昵称（新增 `load_conversation_directory`），一次 `user.db` 解密读取用户映射/账号（新增 `load_user_directory`）。`recent_conversations(refresh)` 与 `read_context` 都从该缓存取名，不再在 `read_context` 里二次解密 `session.db`。
- `_context_cache`：`{conversationId: (at, payload)}`，TTL 10 秒；`read_context(..., refresh=True)` 绕过。手动刷新与首次加载传 `refresh=true`；30 秒轮询使用缓存。
- 预热：首次 `chat.recent` 成功后，后台 daemon 线程调用 `message_source.warmup()`（用缓存中最近会话触发一次消息快照读取），失败静默；避免阻塞查询线程。线程只读快照（内部有 RLock），不写任何状态。
- 分页读取复用 `MessageDatabaseSnapshot`；`before` 页最多读 30 天窗口。

### D5 API 与前端缓存

- `chat.recent` 新增可选 `refresh`，返回补充 `selfSenderId`/`selfSenderName`（本轮仅透出，不在上下文标记「我」）。
- 前端 `contextCacheRef: Map<conversationId, {payload, at}>`，命中时立即渲染旧内容并静默刷新（stale-while-revalidate）；会话切换不再白屏等待。
- 轮询让路：`loadContext` 带请求序号，点击会话时取消/忽略进行中的轮询结果；轮询请求使用缓存参数，不强制 refresh。
- 首屏 `CONTEXT_PAGE_SIZE = 20`；`composeContextText` 仅使用当前已加载消息（不影响生成质量下限）。

### D6 测试与可验证目标

- Python：取名优先级（对方优先、过滤自己名字、重复标题兜底、无法解析→私聊会话）、时间归一化、缓存命中/过期/refresh 绕过、分页 `hasMore` 与 before 边界、预热线程不阻塞且异常被吞、运行时查询参数透传。
- 前端：分页合并去重、滚动到底部/跳到最新、切换会话缓存立即展示、滚动位置保持、名称回退、长消息折叠阈值。
- 发布验收：本地 typecheck / vitest / build:renderer / Python 全量测试通过；UI 预览在 1280×820 与 1024×700 下上下文可滚动、默认最新、无横向溢出；发布 v4.6.4 并验证 Release 资产。

## Risks / Trade-offs

- [缓存陈旧] → TTL 30s/10s + 手动刷新绕过；轮询仍会更新。
- [预热线程与快照并发] → 快照内部 RLock；预热异常全部吞掉，不影响请求。
- [分页重复/跳位] → messageId 去重 + 插入前记录 scrollHeight。
- [窄窗口固定高度不适用] → ≤1279 回退自动高度，保留内部滚动。
- [直聊标题是真实备注但仍被过滤] → 仅在标题等于自己的名字或重复出现时丢弃，其他情况保留标题。

## Migration Plan

无配置或数据迁移；`chat.recent` / `chat.context` 旧参数与返回字段保持兼容。回滚方式为回退到 v4.6.3。

## Open Questions

（无）

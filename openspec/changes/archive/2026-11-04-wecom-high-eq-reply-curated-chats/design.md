# Design

## Context

高情商回复页当前 `chat.recent` 自动返回全部最近会话；「待回复对象」是单个 `targetId`，生成 payload 只带 `text + context`。常驻回复运行时已有 SQLite store（revision/命令幂等）与目录/上下文缓存，适合承载用户自己的会话清单与多消息回复。

## Goals / Non-Goals

**Goals：** 用户维护会话清单（添加/本地改名/移除）、只同步已添加会话、群聊 0~N 条多选综合回复；保持现有只读、仅复制、图片生成时交模型的行为。

**Non-Goals：** 不修改企微数据库；不做企微侧「标星/备注」同步；不做多设备同步；不改图片 OCR/生成风格；不做会话清单手动拖拽排序（按最后消息时间）。

## Decisions

### D1 会话清单持久化

运行时常驻 SQLite 新增表：

```sql
CREATE TABLE IF NOT EXISTS reply_assistant_chats (
  conversation_id TEXT PRIMARY KEY,
  alias           TEXT NOT NULL DEFAULT '',
  added_at        REAL NOT NULL
);
```

通过 `replyRuntimeExecute` 写命令（带 `commandId` + `expectedRevision`，沿用现有幂等与 revision 冲突重试机制）：

- `chat.chats.add`：body `{chats:[{conversationId, alias?}]}`；已存在则仅更新别名（有提供时）。
- `chat.chats.rename`：body `{conversationId, alias}`；空别名视为清除。
- `chat.chats.remove`：body `{conversationIds:[...]}`。
- 全部返回 `{revision}`；前端随后重新查询 `chat.recent`。

校验：conversationId 必须匹配 `^(R|S):` 且长度受限；alias 去空白、最长 40；数组去重；非法项报 `RuntimeProtocolError("INVALID_CHAT", ...)`。

### D2 会话目录查询

新增 `chat.catalog` 查询：

```
request  { kind:"chat.catalog", limit?:300, search?:"" }
response { chats:[{conversationId, displayName, alias, added, lastMessageTime,
                   lastMessageId, isGroup}],
           revision:number, selfSenderId, selfSenderName }
```

- 使用运行时目录缓存（30s / refresh 不影响此查询；目录变化由 TTL 自然刷新），一次读取会话表 + 命名数据。
- `limit` clamp 1..300；`search` 对名称/别名/conversationId 做不区分大小写过滤（也允许前端本地过滤）；按最后消息时间降序。
- `added`/`alias` 从清单表读取；供添加弹窗显示已添加状态。

### D3 `chat.recent` 只返回已添加会话

- 从清单表取所有 conversationId，再用目录缓存补名称与最后消息时间；排序：最后消息时间降序，时间相同按 lastMessageId 降序。
- 返回 `{chats, revision, selfSenderId, selfSenderName}`；每个 chat 带 `alias`、`available`（目录里找不到时为 false，仍保留别名与「会话不可用」提示）。
- 未添加会话不参与轮询；会话清单为空时返回空数组，前端显示引导添加的空态。
- 添加/移除/改名后由前端重新查询；服务端不主动失效上下文缓存（上下文按会话缓存，10s TTL）。

### D4 多消息选择与生成 payload

前端状态：`selected: Array<{messageId, senderName, sendTime, text, imageCount}>`（快照，刷新后不丢），规则：

- 私聊进入会话：默认选中最后一条消息（保留一对一习惯）。
- 群聊进入会话：默认不选；点击消息 toggle 选中/取消。
- 上下文消息展示选中态（边框/勾选），目标面板列出选中消息（最多展示全部，可单条移除/清空）。
- 生成时：
  - `manualMode`：仍只用手动粘贴文本，忽略选中。
  - 否则 payload：`text` = 选中消息文本拼接（供规则回退），`focusMessages` = `[{senderName, timeLabel:"HH:mm", text}]`（最多 20 条），`context` 排除所有选中消息。
  - `imageMessageIds` = 上下文图片 ∪ 选中消息图片，最多 6 条。
- `canGenerate`：手动文本非空，或选中非空，或（群聊且当前上下文消息非空）；全部不满足时禁用并提示。
- 选中消息不在当前已加载窗口时仍按快照参与生成；切换会话清空选择并重新套用默认。

### D5 后端多消息提示词

`assistant_action("eq_reply")` body 新增可选 `focusMessages: [{senderName, timeLabel, text}]`：

- 存在时 prompt 生成「需要回复的消息（共 N 条）」列表（每条 `- [time] sender：text`），并提示「请综合回应以上消息，不要逐条机械回复」。
- 不存在时保持原 `text` 单条逻辑；两条路径都保留 `context`、`recipient`、图片与 JSON 契约。
- 规则回退使用拼接后的 `text`；`text` 与 `focusMessages` 都为空时仍报「请输入需要回复的消息」。

### D6 测试与发布

- Python：表迁移/命令幂等/别名更新/移除、catalog 过滤与 limit、recent 只含已添加（含缺失会话降级）、多消息 prompt 与回退、图片并集。
- 前端：多选 toggle 与默认规则、0 选中生成、选中快照跨刷新、别名显示、添加/移除/改名弹窗交互、payload 与图片并集、缓存/分页不被破坏。
- 发布：v4.6.5，GitHub Actions 构建；UI 预览补浅/深/1024 截图。

## Risks / Trade-offs

- [清单存在运行时 SQLite，配置备份暂不含] → 与现有群监听一致；文档标注，后续可加配置镜像。
- [添加会话后目录缓存 30s 可能短暂不显示新会话] → 添加后手动 refresh 目录（`refresh=true` 或在命令后失效目录缓存）。
- [多选快照与刷新后的消息不一致] → 以 messageId 去重；生成 payload 以快照文本为准。
- [别名与企微备注不同步] → 明确只在本地生效，不写企微数据库。

## Migration Plan

无需迁移旧数据；会话清单初始为空，页面提示添加。回滚到 v4.6.4 后旧自动列表恢复（运行时表保留但不使用）。

## Open Questions

（无）

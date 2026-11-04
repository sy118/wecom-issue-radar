# Design

## Context

「高情商回复」的提示词来自 `config.prompts.items` 中 `scene=eq_reply` 的条目，但后端只取第一条；生成走一次性 Python worker，模型调用在 `worker/reply_assistant/model.py` 中固定 120 秒上限，且带图片失败后会无条件再调一次纯文本。会话清单已持久化在 `reply_assistant_chats`（v4.6.5），可在其中增加每群提示词配置。

## Goals / Non-Goals

**Goals：** 每群选择提示词与角色补充；把单次生成等待压到可接受范围；失败原因与耗时可见；不改变只复制、不发送、图片生成时交模型的边界。

**Non-Goals：** 不做每群完整提示词自由编辑（用提示词库复用）；不改变提示词 JSON 输出契约；不引入流式输出；不做多模型路由（作为后续优化）。

## Decisions

### D1 会话级提示词数据

`reply_assistant_chats` 增加两列（旧库通过 `PRAGMA table_info` + `ALTER TABLE` 迁移）：

```sql
prompt_id   TEXT NOT NULL DEFAULT ''
prompt_note TEXT NOT NULL DEFAULT ''
```

- `prompt_id` 为空表示跟随默认（提示词库中第一条 `eq_reply`）。
- `prompt_note` 为可选的「本群角色补充」，去首尾空白，最多 500 字符。
- 新增写命令 `chat.prompt.set`：body `{conversationId, promptId?, promptNote?}`；只允许已添加会话；字段缺省表示不修改，空字符串表示清除；继承现有 `commandId`/`expectedRevision` 幂等与冲突机制。
- `chat.recent` / `chat.catalog` 返回 `promptId`、`promptNote`。

### D2 提示词解析

`worker/reply_assistant/prompts.py`：

- `configured_eq_reply_prompt(config, prompt_id)`：优先精确匹配 `id == prompt_id` 且 `scene == eq_reply` 的条目；找不到时回退第一条 `eq_reply`；仍为空则使用内置默认。
- `eq_reply_system_prompt(config, prompt_id, prompt_note)`：在基础提示词后追加：
  - 若用户内容没有 JSON 契约标记，继续追加固定输出要求（现有行为）；
  - 有 `prompt_note` 时追加「本群角色补充：{note}」；note 不替换基础提示词。
- `eq_reply_prompt(..., prompt_id="", prompt_note="")` 透传。
- `actions.action_eq_reply` 读取 body `promptId`/`promptNote`，传给 prompts；结果附 `ai: {promptId, promptFallback}` 便于前端提示“提示词已删除，使用默认”。

### D3 生成预算

固定预算（不新增设置项，避免配置膨胀）：

```
总预算 45s
带图片：第一次请求最多 22s
  成功 -> 结束
  失败且属于“不支持图片/图片输入被拒绝” -> 剩余时间最多 18s 纯文本重试
  超时/网络/5xx/JSON 解析失败/空响应 -> 不重试，立即本地模板回退
无图片：单次请求最多 35s
```

- `model.call_llm` 增加可选 `timeout_seconds` 参数；默认仍可用配置超时，但不超过 45 秒。
- `ModelUnavailable` 增加 `kind` 分类：`timeout`、`http_4xx`、`http_5xx`、`network`、`invalid_response`、`empty`、`vision_unsupported`。
- 只有 `vision_unsupported` 才触发纯文本重试；其余失败直接返回本地模板并带原因。
- 原有 `llm.timeout_seconds`（默认 180）不再直接决定回复生成等待时间；回复助手只使用预算。

### D4 图片与上下文限制

- `images.py`：`MAX_IMAGES` 从 6 改为 3，新增总字节上限 12MB；超限跳过并在结果里返回实际附带数量与被忽略数量。
- 页面提供「带图片理解」开关（默认开）；关闭时生成 payload 不包含 `imageMessageIds`。
- 前端 `focusMessages` 上限从 20 改为 10；后端同样限制 10。
- prompt 上下文截断从 4000 字符改为 3000 字符；超长时 UI 显示截断提示。

### D5 耗时与回退原因

`action_eq_reply` 返回新增字段（不影响既有字段）：

```json
{
  "timings": {"imageMs": 12, "modelMs": 8421, "parseMs": 1, "totalMs": 8650, "attempts": 1},
  "ai": {"promptId": "custom_x", "promptFallback": false, "fallbackReason": ""}
}
```

`fallbackReason` 取值：`not_configured`、`timeout`、`vision_unsupported`、`http_error`、`invalid_response`、`empty`、`partial`、`unknown`。`partial` 表示模型返回了部分章节，缺失部分由本地模板补齐。

前端：

- 结果头部显示耗时（AI 或本地模板）。
- `fallback` 时按原因展示文案；`partial` 显示“已用本地模板补齐缺失部分”。
- 未配置模型显示去设置引导；超时显示可重试与减少图片/上下文的建议。
- badge 从「规则版」改为「本地模板」。

### D6 前端提示词配置

- `App.tsx` 把 `bootstrap.config` 传给 `HighEqReplyPage`；页面从 `config.prompts.items` 过滤 `scene === "eq_reply"` 得到选项。
- 生成面板增加「本群提示词」下拉 + 「本群角色补充」输入：
  - 选项：默认 + 每条提示词名称；当前会话绑定值来自 `chat.recent`。
  - 角色补充保存时调用 `chat.prompt.set`，校验 ≤500 字；空值清除。
  - 提示词被删除时显示“提示词已删除，使用默认”，允许改选/清除。
- 生成 payload 新增 `promptId`、`promptNote`。
- 管理入口：以文本链接/按钮跳转提示词库（`strategy-prompts`）。

### D7 测试

- Python：列迁移幂等；`chat.prompt.set` 校验/持久化；recent/catalog 字段；提示词按 ID 解析与回退；note 追加；预算与错误分类（超时不重试、vision 失败重试、总耗时）；图片限制；timings/fallbackReason。
- 前端：提示词选项过滤、payload 字段、角色补充校验、回退文案映射、耗时格式化、图片开关。

## Risks / Trade-offs

- 22 秒图片预算可能截断较慢但可用的图片模型 → 默认提供「带图片理解」开关；计时上线后再按真实数据调整。
- 每群绑定保存在运行时 SQLite，配置备份暂不含该表 → 与「我的会话」清单一致；若需要可后续做配置镜像。
- 提示词被删除时自动回退，用户可能没意识到 → 结果区域明确提示。
- 图片减少可能影响截图理解 → 显示实际附带/忽略数量。

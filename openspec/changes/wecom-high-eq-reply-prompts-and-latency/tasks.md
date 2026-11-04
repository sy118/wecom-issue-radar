# Tasks

## 1. 规划

- [x] 1.1 创建 OpenSpec 变更，完成 proposal / specs / design / tasks 并通过 `openspec-cn validate wecom-high-eq-reply-prompts-and-latency`。

## 2. 后端：会话级提示词

- [x] 2.1 `worker/reply_runtime/store.py` 为 `reply_assistant_chats` 增加 `prompt_id`、`prompt_note` 列及旧库迁移；`chat.recent` / `chat.catalog` 返回 `promptId`、`promptNote`。
- [x] 2.2 `worker/reply_runtime/runtime.py` 增加 `chat.prompt.set` 写命令（已添加会话校验、≤500 字 note、空值清除、revision/幂等）；补测试。

## 3. 后端：提示词解析与角色补充

- [x] 3.1 `worker/reply_assistant/prompts.py` 支持按 `promptId` 精确匹配 `eq_reply` 条目并回退默认；追加 `promptNote`；保持 JSON 输出契约。
- [x] 3.2 `worker/reply_assistant/actions.py` 接收 `promptId`/`promptNote`，返回 `ai.promptId`/`promptFallback`；补单测（绑定、回退、note、默认）。

## 4. 后端：生成预算与回退原因

- [x] 4.1 `worker/reply_assistant/model.py` 支持单次超时参数与错误分类（timeout/http/network/invalid_response/empty/vision_unsupported），不再把 180 秒配置直接用于回复生成。
- [x] 4.2 `actions.py` 重写带图重试：图片 22 秒、纯文本剩余最多 18 秒、总预算 45 秒；仅 `vision_unsupported` 重试；超时/解析失败/空响应直接本地模板。
- [x] 4.3 图片限制：`images.py` 最多 3 张、总字节 ≤12MB，返回实际附带/忽略数量。
- [x] 4.4 返回 `timings`（imageMs/modelMs/parseMs/totalMs/attempts）与 `fallbackReason`；补覆盖超时、带图失败、部分结果、未配置的测试。

## 5. 前端：每群提示词

- [x] 5.1 `src/App.tsx` 向 `HighEqReplyPage` 传入 config；页面过滤 `scene=eq_reply` 生成提示词选项。
- [x] 5.2 生成面板增加「本群提示词」下拉与「本群角色补充」（≤500 字）编辑；通过 `chat.prompt.set` 持久化，切换会话带出，提示词删除时提示回退默认。
- [x] 5.3 `eqReplyPayload` 增加 `promptId`/`promptNote`；补 logic 测试。

## 6. 前端：耗时、原因与图片开关

- [x] 6.1 结果区域显示耗时；「规则版」改「本地模板」；按 `fallbackReason` 展示未配置/超时/图片不支持/返回不完整/网络错误文案。
- [x] 6.2 增加「带图片理解」开关（默认开），关闭时不发送图片；显示实际附带/忽略图片数量。
- [x] 6.3 上下文/多选消息上限收紧（prompt 上下文 3000 字符、focusMessages 最多 10），补截断提示与测试。

## 7. 文档与验证

- [x] 7.1 README / docs 更新每群提示词、角色补充、生成耗时与回退原因、图片开关。
- [x] 7.2 本地门禁：`pnpm typecheck`、`pnpm test`、`pnpm build:renderer`、`python -m unittest discover -s tests`。
- [x] 7.3 UI 预览补提示词选择/耗时/回退文案状态（浅色、深色、1024），确认无溢出。

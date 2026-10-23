# Design

## Context

当前渲染层通过 preload 的 `wechatAction` → IPC `wechat_action` → `MainServices.wechatAction()` → `WorkerClient.runWorkerAction()`，后者为每个请求 `spawn(--request request.json)` 一个新进程。`worker/wechat/service.py` 的 `get_runtime()` 是进程内单例，包含 RuntimeStore、vault、LocalWeChatSource、数据库快照/联系人缓存和轮询状态，但这些状态在一次性进程中每次都重建。

`--reply-runtime`（`worker/reply_runtime/stdio.py` + `electron/main/services/replyRuntime.ts`）已验证常驻协议：

```
stdin:  {id, op: "execute" | "query", payload}
stdout: {id, ok: true, data}
        {id, ok: false, error}
        {type: "event", seq, event}
```

本变更复用该模式为个人微信建立独立的常驻通道。

## Decisions

### 1. 常驻 worker 作为主通道

新增 `worker/wechat/stdio.py`：读取 NDJSON，逐行调用 `handle_wechat_action(action, payload)`，输出与 reply-runtime 相同的 id 关联响应。`worker/main.py` 增加 `--wechat-runtime`。

Electron 新增 `WechatRuntimeSupervisor`（参考 `replyRuntime.ts`）：

- 首个 `wechat_action` 时懒启动，避免启动应用就拉起重型 Python；
- 请求 id 关联、默认超时（读 30s、写 60s，图片预览单独 30s）；
- 崩溃后 100ms → 5s 指数退避重启，最多连续 N 次后退回一次性 worker；
- 应用退出、更新安装前 `shutdown()`，等待当前请求完成后终止进程树；
- stdout 只解析 NDJSON，日志/异常走 stderr；错误统一映射为请求的 `ok:false`；
- 保持渲染层 API 不变：`wechat_action` 在 `MainServices` 内改为 supervisor 优先，supervisor 不可用时自动 fallback 到 `WorkerClient`。

### 2. 批量首屏接口

新增 `wechat.bootstrap` 动作，payload 可带 `projectId`、`messageLimit`：

```json
{
  "detect": {...},
  "projects": [...],
  "settings": {...},
  "providers": {...},
  "messages": [...]  // 仅当提供 projectId
}
```

渲染层首屏从 5-7 个请求降为 1 个；`detect` 已配置目录时复用缓存，不做全量扫描。图片仍按需 `media_preview`（批量 base64 会把首屏 payload 放大，不纳入 bootstrap）。

### 3. 并发与副作用

- 读操作（messages/media/projects/settings）可在常驻进程内并发，但 source 的数据库访问通过既有 `_source_guard` 串行。
- 写操作（project_save/delete、draft_save、settings_save、key scan）按到达顺序串行执行，返回明确结果；不自动重试写请求，避免重复删除/重复保存。
- 图片预览由前端并发上限（已存在于 v4.3.0）保护，worker 端不做无限队列。

### 4. 降级与兼容

- 常驻启动失败（ENOENT、权限、崩溃超过阈值）→ 记录一次可读诊断并回退一次性 `--request`，功能不丢，只是恢复旧性能。
- `--request` 路径保留，用于 `--extract-keys`、自检和 supervisor 不可用的场景。
- 消息协议新增字段向后兼容；旧版本 worker + 新 Electron 不应发生（同一安装包），但一次性通道仍返回原有 JSON 结构。

### 5. 可观测性

- supervisor 记录启动耗时、重启次数、当前 in-flight 请求数、最近错误码；
- 首屏 bootstrap 记录耗时分解（detect/projects/settings/messages）；
- 设置/诊断页可展示 worker 模式（常驻/一次性）与最近启动耗时，便于现场排查。

## Risks / Trade-offs

- 常驻进程会长期占用内存（Python + 微信库），但远小于每次启动的解包/导入成本。
- 需要处理 Python 进程退出、stdin 半关闭、请求超时和孤儿进程；复用 replyRuntime 的成熟逻辑降低风险。
- 批量 bootstrap 扩大了单次响应体；限制消息条数（默认 50）并禁止内联图片。
- 写操作串行可能让并发保存排队；个人微信是单用户操作，可接受。

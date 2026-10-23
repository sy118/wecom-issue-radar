# Proposal

## Why

v4.3.0 已经消除了反复 backlog 和重复挂载，但个人微信的每个 `wechat_action` 仍会启动一个新的 PyInstaller onefile Python 进程。首次进入会话仍然要启动 projects、detect、settings、providers、messages、poll 和可见图片等多个 sidecar；单次启动包含解包、导入、运行时初始化，叠加后仍是秒级到十几秒的等待。

现有 `--reply-runtime` 已经验证了常驻 sidecar + NDJSON 请求/响应协议的可行性，但个人微信仍走一次性 `--request` 通道。需要把个人微信动作迁移到常驻 worker，并用批量首屏接口减少第一次会话的往返次数。

## What Changes

- 新增 **常驻个人微信 worker** 模式（`--wechat-runtime`）：一个 Python 进程内复用 `get_runtime()`、`LocalWeChatSource` 缓存、游标与轮询状态，处理多个带 id 的 JSON 请求。
- Electron 主进程新增 `WechatRuntimeSupervisor`：按需启动、请求/响应关联、超时、崩溃重启退避、退出/更新时优雅关闭；渲染层 `wechatAction` API 不变，主进程内部优先走常驻 worker，失败时回退一次性 worker。
- 新增 **批量首屏接口** `wechat.bootstrap`：一次返回检测、项目、设置、模型/MCP 和（可选）选中项目的最近消息，减少首次进入的往返次数。
- 常驻模式下的写操作（项目保存/删除、草稿保存、设置保存）必须可重试且幂等边界清晰；图片预览、消息读取可以并发，但同一 runtime 的数据库访问保持串行。
- 保留一次性 `--request` 作为降级路径（常驻进程启动失败、被防火墙/杀软拦截、端口不可用时）。

## Capabilities

### New Capabilities

- `personal-wechat-runtime`: 个人微信常驻 worker 的生命周期、请求协议、批量首屏接口、并发/超时/降级与可观测性。

### Modified Capabilities

无（个人微信工作台的行为保持不变，只改变其运行时通道）。

## Impact

- `worker/main.py`、新增 `worker/wechat/stdio.py`（或复用 reply-runtime 的 NDJSON 基础组件）。
- `worker/wechat/actions.py`：新增 `bootstrap` 批量动作；现有动作保持兼容。
- `electron/main/services/`：新增 `wechatRuntime.ts` 监督器，`index.ts` 路由 `wechat_action`；`ipc.ts`/contracts 仅在需要新命令时扩展。
- `src/pages/wechat/useWechatWorkbench.ts`：首屏改用 bootstrap，图片/消息仍按需请求。
- CI：新增常驻 worker 的 Python 单测与 Electron 监督器单测；打包仍是一个 sidecar exe。
- 风险：常驻进程的生命周期、崩溃恢复、更新安装前关闭、并发副作用顺序需要严格测试；不改变微信原始数据库只读边界。

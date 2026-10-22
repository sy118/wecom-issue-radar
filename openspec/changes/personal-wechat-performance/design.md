# Design

## Context

个人微信渲染层每个动作都通过 Electron 的 `WorkerClient.executeRequest()` 启动一个新的 PyInstaller onefile Python 进程。进入会话页会同时触发 projects、detect、settings、providers、messages、poll 以及可见图片预览等多个进程；轮询状态 `_poll_initialized` 又只保存在单个进程内，导致每次 poll 都以 `backlog=True` 重新读取窗口内消息。引用消息解析只取 `refermsg/content` 原文，图片/表情引用会显示 XML。

## Decisions

### 1. 轮询增量而不是每轮 backlog

`poll_once` 不再依赖进程内布尔值决定是否 backlog。数据源本身已经按项目/表检查游标是否存在（`_read_chat_table` 的 `not initialized` 分支），因此 `poll_once` 固定传 `backlog=False`；新项目仍会因为没有游标而读取最近窗口，已初始化项目只读游标之后的数据。

### 2. 会话页保持挂载

`App` 首次进入个人微信后保留 `WechatWorkbenchPage` 实例，用 `hidden` 切换显示；页面接收 `active`，不可见时不轮询。这样再次进入直接使用已有 projects/messages/settings 状态，同时避免后台定时器继续启动 worker。

### 3. 首屏请求编排

- `detectWechat` 成功后不再自动调用 `listWechatChats`；会话列表在打开项目编辑器时按需加载。
- 消息加载完成后才开始第一次 poll，轮询间隔从 10s 放宽到 30s；页面不可见时跳过。
- 图片预览通过模块级并发队列限制最多 2 个同时请求，LRU 缓存扩大到 64 条。

### 4. 引用消息解码

`parse_message` 读取 `refermsg/type` 与 `refermsg/content`：

| 引用类型 | 展示 |
| --- | --- |
| 3 / 47 | `[图片]` / `[表情]` |
| 34 | `[语音]` |
| 43 | `[视频]` |
| 49 | `<title>`，否则剥标签 |
| 文本 | 原文；仍像 XML 时显示 `[非文本消息]` |

### 5. 本地项目删除

左侧项目行增加删除按钮，删除前弹出应用内确认（不删除微信原库）。前端调用既有 `project_delete`，同时清理当前页面的 messages/drafts 缓存；配置文件中的其它项目不受影响。

## Risks / Trade-offs

- 保持挂载会增加常驻内存（一个已加载会话的 message state），但远小于反复重拉与重启 sidecar 的成本。
- 轮询固定 `backlog=False` 假设游标可靠；若本地游标损坏导致漏消息，用户可通过“刷新消息”触发显式重新读取。
- 图片并发限制会让大量图片逐步出现，但首屏交互更流畅。

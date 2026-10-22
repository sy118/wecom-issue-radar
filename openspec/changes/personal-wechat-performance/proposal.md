# Proposal

## Why

个人微信「会话草稿」进入慢、引用消息显示原始 XML、本地项目删除入口缺失，已经在实际使用中反复出现。根因集中在：每次 IPC 动作都启动新的 Python sidecar、轮询状态只存在进程内导致每轮都按“首次”重扫、图片预览并发起多个进程，以及后端没有解码引用消息的类型。需要一次聚焦的体验修复，让会话页可以快速打开、引用消息可读、本地项目可管理。

## What Changes

- 轮询改为真正的增量：不再把 `poll_initialized` 放在每次会丢失的 Python 进程内，已初始化的会话只读取游标之后的消息。
- 会话页在应用内保持挂载：离开再进入不再重新拉取项目、设置、消息；页面不可见时暂停轮询。
- 首屏减少并发 worker：检测到已配置目录后不再因 detection 附带加载会话列表；消息加载完成前不立即轮询。
- 图片预览限制并发并扩大缓存，避免一屏图片同时启动多个 sidecar 进程。
- 本地会话项目支持在左侧列表直接删除：删除项目、本地消息缓存、草稿和游标，不修改微信原始数据库。
- 引用消息按 `refermsg/type` 解码为 `[图片]`、`[语音]`、`[视频]`、`[链接]` 或纯文本，不再展示原始 `<msg><img ...>` XML。

## Capabilities

### New Capabilities

- `personal-wechat-workbench`: 个人微信会话工作台的进入性能、增量同步、本地项目生命周期、引用消息解码与图片预览行为。

### Modified Capabilities

无。

## Impact

- `worker/wechat/wechat_source.py`：轮询 backlog 语义、引用消息解析。
- `worker/wechat/service.py` / `actions.py`：轮询状态与消息返回。
- `src/pages/wechat/useWechatWorkbench.ts`、`src/pages/wechat/WechatWorkbenchPage.tsx`、`src/components/wechat/ProjectRail.tsx`、`src/App.tsx`：首屏请求编排、页面保持挂载、项目删除、图片并发。
- 仅影响本机个人微信功能，不涉及企业微信、发布目标或微信原始数据写入。

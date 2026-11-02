# Proposal

## Why

「沟通助手」是一个与真实沟通场景脱节的独立板块：回复助手要求用户手动粘贴消息，话术库和长文速读又各占一个页面；而应用本身就只读本机企业微信数据库，完全可以自动带出"最近聊了什么"。用户真正需要的是：在企微里选中一个最近会话，直接看到上下文，对最后一条消息生成高情商回复并复制。同时，沟通助手里大量重复代码（三个页面、daily_action IPC、summarize/phrases 后端）增加了维护面。

本次把沟通助手整体删除，只把回复助手的能力迁移到企业微信的「高情商回复」页面，并补上自动上下文与图片 OCR。

## What Changes

- **BREAKING：删除「沟通助手」板块**：移除回复助手 / 话术库 / 长文速读三个页面与入口、`daily_action` IPC、`worker/daily` 中的 summarize / phrases 能力与相关测试。`phrases.json` 数据文件保留在本机，不迁移、不读取，也不删除。
- **新增「企业微信 → 高情商回复」页面**：
  - 自动列出最近活跃的群聊与私聊会话（按最后消息时间排序，默认选中最近一条），也可手动切换；
  - 自动读取所选会话最近上下文（默认最近 30 条 / 6 小时，只读），页面可见时每 30 秒轮询 + 手动刷新；
  - 默认选中最后一条消息作为"待回复对象"，用户可点击上下文中的任意消息切换；
  - 图片消息自动 OCR 后进入上下文：优先使用「设置 → 模型与 OCR」的独立 OCR 模型；未配置 OCR 但全局模型支持图片时，回退用全局视觉模型逐张识别；结果按图片缓存，每轮最多 6 张、单张 60 秒、总预算 45 秒，失败降级为 `[图片 OCR 失败]`，识别文本在气泡中可展开查看；
  - 生成「潜台词解读 + 稳重/亲和/简短三版 + 发送前检查」，结果可编辑，**仅复制到剪贴板**，不自动发送；
  - 提示词使用「策略与发布 → 提示词库 → 高情商回复（eq_reply 场景）」中可编辑的内容；未配置提示词时使用内置默认提示词；
  - 未检测到企微数据/密钥、会话为空、未配置模型、OCR 未配置等状态都有明确空态与引导。
- **后端**：常驻回复运行时新增 `chat.recent` / `chat.context` / `chat.ocr` 查询；`worker/ocr.py` 抽出可复用的单图 OCR 与缓存；`worker/daily` 的 eq 能力迁移到 `worker/reply_assistant`；IPC `daily_action` 改为 `assistant_action`。
- **文档与发布**：README / docs / CHANGELOG 同步删除与新增内容；版本升到 `4.6.0`，由 GitHub Actions 在线构建并发布 Release（本机不打包）。

## Capabilities

### New Capabilities

- `wecom-high-eq-reply`: 企业微信内的高情商回复：最近会话与上下文自动读取、图片 OCR 进入上下文、可编辑提示词、生成三版回复与解读/检查、仅复制。

### Modified Capabilities

（无：现有 `desktop-ui-system` 与 `model-configuration` 的需求不变；沟通助手原本没有对应 spec。）

## Impact

- **删除**：`src/pages/daily/**`、`src/components/daily/**`、`worker/daily/`、`tests/test_daily_actions.py`、`daily_action` IPC 与服务方法、导航/路由/工作台快捷入口/提示词页场景中的沟通助手条目与相关测试。
- **新增/修改后端**：`worker/reply_runtime/runtime.py`（新查询）、`worker/reply_runtime/message_source.py`（上下文读取与图片描述符复用）、`worker/ocr.py`（单图 OCR + 缓存 + 视觉回退）、新建 `worker/reply_assistant/`、`worker/main.py`、`worker/issue_radar_worker.spec`、`electron/shared/contracts.ts`、`electron/main/ipc.ts`、`electron/main/services/index.ts`、`src/lib/bridge.ts`。
- **新增/修改前端**：新页面与样式、`src/navigation.ts`、`src/App.tsx`、`src/types.ts`、`src/pages/PromptsPage.tsx`、`src/pages/WorkbenchPage.tsx`、`scripts/ui-preview.mjs`、`docs/images/`。
- **文档**：`README.md`、`CHANGELOG.md`、`docs/README.md`、`docs/electron-migration.md`、`docs/release.md`（版本示例）。
- **兼容性**：`phrases.json` 保留但不再使用；旧配置无需迁移；个人微信、问题雷达、定时任务、群监听不受影响。

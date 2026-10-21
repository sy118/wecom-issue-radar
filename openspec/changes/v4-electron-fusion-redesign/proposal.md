# Proposal: v4.0.0 Electron 融合版

## Why

v3.5.3 是一个只会做"企微群聊导出"的 Tauri 桌面工具，当前有三个同时爆发的问题：

1. **桌面壳与技术栈需要换代**：用户明确要求下一版改用 Electron。继续维护 Tauri 壳意味着 UI 现代化、字体渲染与打包发布都受限于两套工具链。
2. **上手成本高到劝退小白**：全站 298 条 `font-size` 中 274 条小于 14px（92%）、210 条小于等于 10px（70%）；侧边栏 7 个入口里 4 个是配置概念；设置页 29 个字段、配置文件约 130 个可调项；首启没有向导，事实上的引导是 README 的 9 个步骤。
3. **能力被拆在两个项目里**：个人微信的读取、草稿、填入能力在独立的 ProxyTalk（V0.0.3）中；而用户每天真正高频的"找一段聊天、回一条难回的消息、写周报"没有入口。与此同时企微图片缺失在 2026-07-24 至 08-26 间反复修了 10 次仍未收敛，需要从架构上收口并给出诚实的降级。

本次变更把上述四件事收敛成一个可独立回滚的四阶段版本 v4.0.0：M0 换壳保功能，M1 重做体验与策略解耦，M2 融合个人微信，M3 上线日常 AI。

## What Changes

- **桌面壳迁移 Electron（BREAKING）**：Tauri 2 换为 Electron 主进程（TypeScript）+ preload contextBridge；渲染层继续 React + Vite。安装包由 NSIS + MSI 变为仅 NSIS，安装包体积约 90–120MB，旧 Tauri 版本无法原地自动升级，需手动迁移。
- **进程与数据模型收敛**：单 Python sidecar（`--request` / `--reply-runtime` / `--extract-keys` 三种模式，协议不变）；配置归一化下沉 Python；调度器用 TypeScript 重写；微信"填入"保留为去 Tauri 的 Rust helper；数据目录继续使用 `%USERPROFILE%\.wecom-issue-radar`，已安装用户零迁移。
- **信息架构与可读性重做**：顶部板块拆为工作台 / 企业微信 / 个人微信 / 日常助手 / 策略与发布 / 设置；企业微信与个人微信视觉与导航彻底分开；全站字号 token 化（正文 16px、最小 12px、控件 44px、WCAG AA 对比度）；提供 100/110/125% 界面缩放；设置收敛为"常用 6 项 + 高级"。
- **首启引导**：三条路径向导（只导出群聊 / 群内 AI 回复 / 个人微信草稿），自动检测数据目录、密钥与模型能力，进度可恢复，自动发送默认关闭。
- **提示词与发布目标解耦**：提示词升级为独立的"AI 策略中心"（问题分析 / 回复 / 周报 / 高情商等场景），发布目标成为可插拔模块（Excel、Markdown、腾讯文档，预留飞书），任务与定时任务分别选择"提示词 + 发布目标"。
- **融合 ProxyTalk**：定义统一 `ChatSource` 契约（企微群 / 个人微信），个人微信支持只读会话、项目绑定、草稿生成（三风格改写）、复制或填入空输入框（永不自动发送）、图片解密、导出、只读 Skill 与 MCP；模型连接统一为一份（OpenAI-compatible / Anthropic + 能力探测 + DPAPI 凭据）。
- **企微图片部分可解**：抽公共媒体解析层，统一日报与回复链路的图片定位/等待/补扫；缺图显式占位并提供"重新读取"；导出与腾讯文档同步标注缺失且不阻断。不承诺 100%，不做服务端取图。
- **日常助手**：P0 上线跨企微与个人微信的本地全文检索（独立 SQLite FTS5 索引）与高情商回复（稳重/亲和/简短，只复制或填入）；P1 周报/日报与待办提取；P2 未读摘要、群内问题速查、话术库。
- **构建与发布**：GitHub Actions 重写为 electron-builder 流程；main 推送产出未签名快照 artifact，`v4.0.0` 标签产出正式 Release 与 `latest.yml`；每个 Release 保留固定版 `latest.json` 使旧 Tauri 客户端不报错。
- **明确不做**：个人微信/群聊自动发送、多账号、飞书真实对接、MSI、移动端、服务端取企微图片、复制 WeChatDataAnalysis 的代码或 native 二进制（该项目无 LICENSE）。

## Capabilities

### New Capabilities

- `desktop-shell`: Electron 主进程与 preload 边界、窗口与单实例、数据目录、Python sidecar 与 Rust helper 生命周期、自动更新、旧 Tauri 版本检测与迁移提示、安全基线。
- `app-experience`: 新信息架构与导航、三路径首启引导、字体/行高/控件尺寸/对比度规范、界面缩放、设置常用/高级分组。
- `wecom-media`: 企微图片的统一定位、等待与补扫，缺图的显式状态与降级，导出/同步中的缺失标注与重试。
- `chat-sources`: `ChatSource` 契约、消息归一模型、企微群与个人微信来源注册、只读与媒体解密边界。
- `personal-wechat-workbench`: 个人微信会话与项目、草稿生成与改写、复制/填入但不发送、导出、Skill 与 MCP 授权、模型连接。
- `ai-strategy-center`: 提示词库（场景化）、发布目标可插拔、任务/定时与两者解耦绑定、凭据与配置迁移。
- `daily-assistant`: 跨源聊天查找（本地 FTS5 索引）、高情商回复、周报/日报、待办与承诺提取。

### Modified Capabilities

无。项目当前没有既有 specs；既有企微导出、定时、群监听行为以现有测试为回归保护，不在本次变更中重新定义。

## Impact

- **代码**：新增 `electron/**`（main/preload）、`build/`、`electron-builder.yml`、`src/lib/desktop.ts`、Rust `wechat-input-helper` crate；删除 `src-tauri/**` 与 `@tauri-apps/*` 依赖；`worker/**` 增加 `chat_sources/`、`wechat/`、`search/`、`strategies/`、`daily/`；前端 `src/**` 按新 IA 重排。
- **依赖**：Electron、electron-builder、electron-updater；Python 合并 `zstandard`、`silk-python`，`mcp` 统一到 `>=1.13,<2`，`pycryptodome>=3.23`；PyInstaller spec 合一。
- **数据**：`%USERPROFILE%\.wecom-issue-radar\` 保持（config.local.json、reply-runtime.sqlite3、schedule-state.json、logs）；新增 `chat_search_index.db`；ProxyTalk 的 `%APPDATA%\ProxyTalk` 一次性导入且不删除原库。
- **配置**：`config_version` 3 → 4，仅注入缺省与迁移，不改旧键；备份格式 v2 只存 `secret_ref`。
- **发布**：版本号唯一来源 `package.json`；删除 Tauri/Cargo 版本校验与 MSI 产物；旧 `latest.json` 固定指向最后一版 Tauri 安装包。
- **测试**：前端 18 个测试中仅 `bridge.test.ts` 需改 mock，`appUpdater.test.ts` 因 adapter 边界不动；Python 23 个测试保持；`test_release_metadata.py` 的 Tauri 断言重写；新增字号下限、对比度、导航与索引相关测试。
- **兼容性**：旧 Tauri 用户需手动卸载后安装 v4，业务数据不丢失；v3.x 进入只做安全维护状态。

# Design: v4.0.0 Electron 融合版

## Context

现状与约束（动机参见 proposal.md）：

- 两个项目同栈：Tauri 2 + React + Rust 壳 + Python sidecar。wecom v3.5.3 的 Rust 壳约 230KB（config 64KB、scheduler 98KB、reply_runtime 48KB、worker 12KB、commands 12KB）；ProxyTalk V0.0.3 的 Rust 壳约 19KB，其中 `input_bridge.rs` 是唯一无法用跨平台方式替代的 Windows UI Automation 能力。
- 两个 Python worker 已各自成熟：wecom 的 `reply_runtime`（20+ 张表、持久化、lease、事件序号、更新互锁）与 ProxyTalk 的 `DraftRuntime`（项目/草稿/消息/导出）。协议都是 NDJSON，但请求形状不同。
- 数据位置与壳无关：wecom 全部业务数据在 `%USERPROFILE%\.wecom-issue-radar`，ProxyTalk 在 `%APPDATA%\ProxyTalk`；换壳不需要迁数据，但 Electron 的 Chromium profile 无法复用 WebView2 缓存（仅丢失主题等 localStorage）。
- 本机只有 Node v24 / pnpm 10.13.1，没有 Python、Rust、node_modules；验证只能在 CI 完成构建，前端单测与类型检查可在本地跑。
- 现有测试基线：前端 18 个测试文件、Python 23 个；Rust 测试随壳删除，UIA helper 保留单测。
- 参考项目 WeChatDataAnalysis 无 LICENSE（默认保留所有权利），只借鉴索引工程思路，不复制代码或 native 二进制。

## Goals / Non-Goals

**Goals**

- 换壳期间业务行为对用户连续：配置、数据、导出、定时、群监听在 M0 后与 v3.5.3 等价。
- 一处协议、一个常驻进程：避免双 sidecar 带来的双数据库、双事件序号与更新互锁复杂度。
- 把"个人微信"接成与"企微群"同级的来源，而不是并列的第二套应用。
- 让配置项数量对用户不可见：常用六项 + 高级折叠，迁移自动完成。
- 所有构建与发布只走 GitHub Actions，本地不产生正式产物。

**Non-Goals**

- 不把两个 `runtime.py` 合并成一个大文件（356KB 内核回归不可控），只做接口与数据层收敛。
- 不改动企业微信/个人微信原始数据库，不实现自动发送。
- 不在 v4.0.0 内做飞书真实对接、多账号、MSI、移动端与 i18n。
- 不重写全部 134KB CSS，只做分层 token 化与组件级替换。

## Decisions

### D1：Electron 采用"渐进双壳 + 壳内全 TypeScript"

v3.5.3 冻结为维护标签，v4 在同一仓库新增 `electron/**`；M0 验收后删除 `src-tauri/**`。壳内用 TypeScript 重写窗口、IPC、调度、进程管理与更新；Python 与 Rust helper 作为 resources 随包分发。

- 备选 A（一次性整体重写）：回归面大、无法用 v3 做回滚参照，否决。
- 备选 C（Electron + 保留 Rust core 二进制通过 stdio 提供全部命令）：能省 config/scheduler 重写，但多一个进程与协议，仍需剥离 Tauri 依赖，收益不抵成本，否决。

### D2：单 Python sidecar，配置归一化下沉 Python，调度器留在 TypeScript

- Python 单进程支持 `--request`（一次性任务）、`--reply-runtime`（常驻）、`--extract-keys` 三种模式，协议保持 `{id, op: execute|query, payload}`；新增 `config.*`、`chat.*`、`search.*`、`strategy.*`、`daily.*`、`job.*` 命令族。
- 配置的 deep merge、v3→v4 迁移、备份脱敏、受保护键恢复下沉到 Python（复用 `worker/pipeline/config_store.py`）；TS 只负责路径解析、原子读写与备份文件外壳。
- 调度器用 TS 重写：30s tick、`lastTick` 超过 90s 时补一次追赶、沿用现有 `schedule-state.json` 格式（临时文件 + fsync + rename，保留 500 条、50/页），不改 claim 语义。
- 备选：调度器下沉 Python —— 与常驻 sidecar 生命周期耦合，sidecar 崩溃即停调度，否决；node-cron —— 休眠唤醒与漂移不可控，否决。

### D3：微信"填入"保留为独立 Rust helper

`input_bridge.rs` 去 Tauri 化，编成 `wechat-input-helper.exe`，通过 stdio 单行 JSON 接收填入请求，保留 8 秒窗口/标题/焦点/空输入框校验链。

- 备选 koffi/ffi-napi 直接调 COM IUIAutomation：需手搓 vtable/BSTR/SAFEARRAY，且升级 Electron 需重编，风险过高。
- 备选 PowerShell + .NET UIAutomation：启动慢、AMSI 易误报、编码坑多，仅作兜底。

### D4：统一 ChatSource 契约与单业务库

- `ChatSource`：`inspect / watermark / read(cursor) / refreshMedia / close`，游标不透明；消息归一为 `ChatMessage`（source、account、chat、chat_kind、sender、direction、kind、text、quote、media、mentioned_me、ts、seq、local_id、source_key）。
- 数据：沿用 `reply-runtime.sqlite3`，个人微信数据用 `wechat_*` 前缀表（projects / messages / drafts / cursors / media_refs），不引入第二个业务库；ProxyTalk 的 `proxytalk.sqlite3` 通过一次性导入器搬入并在原库保留。
- 幂等：企微沿用 inbox UNIQUE；个人微信用 `(source_db, sort_seq, local_id)` 稳定键。
- 备选：双库并存 —— 破坏 revision/lease/事件序号一致性，否决。

### D5：检索使用独立 FTS5 索引库

新增 `chat_search_index.db`，`msg` 表 + `msg_fts` 虚表；写入前把中文转"字符+空格"再喂 unicode61，查询同法转换；`index_cursor` 按来源与会话增量，tmp 文件 `os.replace` 原子替换，损坏可整库重建。

- 备选：直接对源库 LIKE —— 受加密快照与微信占用限制，分钟级且不可增量，否决。
- 备选：jieba 分词 —— 词典体积与召回不可控，P1 再评估 trigram。

### D6：配置 v4 与发布目标注册表

```yaml
config_version: 4
sources: { wecom: {...}, wechat: {...} }
models: { connections: [{id, name, kind, base_url, model, secret_ref, capabilities}], default_id }
ai_strategies: [{id, name, scene, content, fields}]        # 问题分析/回复/周报/高情商
publish_targets: [{id, name, kind, config, secret_ref}]    # excel/markdown/tencent_smartsheet/预留
bindings: { tasks: [{prompt_id, publish_target_id}], schedules: [...] }
daily: { search: {...}, weekly: {...} }
```

v3→v4 只注入缺省、不改旧键；保存时把旧 `smart_sheet.templates` 映射为 `publish_targets` 的一条；备份 v2 只存 `secret_ref`。

### D7：字体与可读性用四层迁移，不推倒 CSS

L0 `:root` tokens（字号/行高/间距/对比度）→ L1 基础元素 → L2 基础组件 → L3 页面只改字号与间距；验收 `grep font-size ≤11px = 0`。界面缩放用 `webFrame.setZoomFactor` 并持久化，与 CSS token 不叠乘。

### D8：发布与更新迁移

- electron-updater + GitHub provider；NSIS-only；`autoDownload=false`，按"准备（停任务/停监听）→ 下载 → quitAndInstall"推进，失败恢复。
- 每个 Release 同时上传 `latest.yml`（Electron）与固定版 `latest.json`（指向最后一版 Tauri 安装包），旧客户端不报错也不误装。
- 版本号唯一来源 `package.json`；删除 Tauri/Cargo/tauri.conf 三处校验。
- 备选：保留 Tauri 双发布 —— 维护成本翻倍，否决。

### D9：安全基线

`contextIsolation: true`、`sandbox: true`、`nodeIntegration: false`；preload 白名单暴露 23 个既有命令 + 新命令；`ipcMain` 校验 sender；CSP 禁止远程脚本；文件命令限制在对话框返回值或数据根内；日志沿用现有 key 归一化脱敏。

## Risks / Trade-offs

| 风险 | 缓解 |
|---|---|
| 调度器休眠/唤醒漏跑、时钟漂移 | 30s tick + 90s 追赶窗口 + 当日 claim 落盘；执行历史保留可对账 |
| PyInstaller onefile 父子进程杀不干净 | 用 Job Object 或 `taskkill /T`，退出路径统一清理 |
| 配置迁移丢密钥或旧任务引用 | 只注入缺省、不改旧键；备份 v2 脱敏；迁移前自动备份并在首启提示 |
| 更新中断、旧 Tauri 与 Electron 双安装 | 分段 prepare/download/install；首启检测旧安装；Release 保留兼容清单 |
| UIA 误填到错误窗口 | 保留 8 秒四重校验；失败只复制不填 |
| 搜索索引损坏或占用磁盘 | 独立库 + 原子替换 + 一键重建；索引只存文本与元数据 |
| 安装包变大、首启变慢 | 首启进度提示；Electron 与依赖缓存；sidecar 懒加载 |
| 图片仍可能缺（企微未落盘） | 显式原因码 + 占位 + 补扫；对外只承诺"部分可解" |
| 参考项目许可证 | 只吸收思路，不引入其代码/资源/native 二进制 |

## Migration Plan

1. **M0 换壳保功能（可回滚）**：打 v3.5.3 维护标签；新增 `electron/**` 与 `electron-builder.yml`；移植 23 个命令与 4 类事件；config/scheduler/进程管理落地；CI 改为 electron-builder；旧安装检测提示。门禁：前端 18 + Python 23 测试绿、CI 产出 NSIS、企微主流程手工回归。回滚：重装 v3.5.3，数据不受影响。
2. **M1 体验与策略（可回滚）**：新 IA、三路径引导、字号 token 化与缩放、设置精简、提示词/发布目标解耦、图片 A+B。回滚：切回 M0 版本；配置向后兼容。
3. **M2 融合（可回滚）**：ChatSource 与个人微信接入、一次性导入 ProxyTalk 数据、草稿/复制/填入、导出、Skill/MCP。回滚：隐藏个人微信板块，原库未删除。
4. **M3 日常 AI（可回滚）**：FTS5 索引、聊天查找、高情商回复；P1 周报/待办随后。回滚：功能开关关闭，索引可删。
5. **发布**：全部门禁通过后打 `v4.0.0`；Release 同时上传 NSIS、`latest.yml`、兼容 `latest.json`；旧用户按首启提示手动迁移。

## Open Questions

- Electron 主版本是否固定为 CI 中的某个 LTS（实现时按 electron-builder 兼容性锁定，不影响本设计）。
- v4.0.0 是否购买 Authenticode 签名（不改变设计；不签名则 SmartScreen 提示与现状持平）。

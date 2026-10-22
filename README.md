# 企微问题雷达

> 本桌面程序参考陈俊宏同学的《AI群咨询日期级自动拉取、识别、登记 WeCom Daily Issue Pipeline v2》思路，再次感谢原作者的开源。

一个面向业务、产品、运营和客户成功团队的 Windows 桌面工具：从本机企业微信群聊中提取指定时间范围的聊天记录与截图，用 OCR / 大模型整理成问题清单，导出 Excel、Markdown，或同步到腾讯文档 Smart Sheet；同时提供个人微信会话草稿、群监听自动拟稿、回复助手、话术库和长文速读。聊天数据、配置和处理结果默认都留在本机。

当前版本 **v4.2.1**，桌面壳使用 **Electron 45**（`v3.5.3` 是最后一个 Tauri 版本，之后只做安全维护）。最新安装包见 [Releases](https://github.com/sy118/wecom-issue-radar/releases)。

## 主要变化（v4.2）

- **键盘与可访问性（v4.2.1）**：Ctrl/Cmd+K 快速跳转、Ctrl/Cmd+S 保存提示词、Ctrl/Cmd+Enter 生成助手结果；弹层统一 Esc 关闭、焦点陷阱与关闭后焦点恢复；未保存修改改为应用内确认框；页面切换自动回到顶部并聚焦主内容。
- **做减法**：删除聊天查找、周报总结、待办与承诺；前后端代码同步清理，不再维护重复功能。
- **群监听**：只保留 Agent V2，删除 Legacy；删除 MCP 工具逐项人工授权，已启用 MCP 服务的**只读工具自动可用**，写/破坏性工具不暴露、不可调用。
- **沟通助手**：回复助手（潜台词解读 + 三版回复 + 发送前检查）、话术库、长文速读。
- **个人微信**：新增独立配置中心、数据目录自动检测、自定义回复风格；删除重复的“永不自动发送”提示。
- **发布目标**：腾讯文档恢复并升级字段映射，支持转换参数、目标字段 Schema 编辑、旧模板导入和提示词字段联动。
- **界面框架**：统一 Portal 弹层，修复“框架套框架”、双滚动和缩放裁切。

## 功能

### 企业微信

- 自动检测企业微信安装路径与数据目录；支持注册表安装路径、运行中进程路径、多盘符、OneDrive 和多级账号目录，按数据库最近修改时间排序。
- 读取本地数据库中的群聊列表，按起止日期、分钟级跨天区间和多个群处理。
- 只导出原始聊天，或启用 OCR + 大模型问题提炼。
- 同时支持 Excel、Markdown 和腾讯文档 Smart Sheet。
- 可创建定时任务：执行时间、每周执行日、动态当天/前一天/固定日期、多群和处理方式；单群无记录或失败不阻断后续群。
- 本地保存定时任务执行记录，可分页筛选并查看成功、部分完成、无记录、失败及各群详情。
- 群监听回复：只支持 Agent V2；同一群最多一个启用监听器，同一发送者串行；合并连续补充、等待群友回答、支持 MCP Agent 或 Dify Chatflow。
- Agent V2 支持多轮补查、最高 200 轮 / 200 次工具调用、上下文压缩、检查点恢复、Token 统计和持久化执行时间线。
- MCP 服务可配置 SSE、stdio、Streamable HTTP；只读工具自动可用，写/破坏性/未声明工具不会进入工具列表。
- Dify 接入支持登记多个已发布 Chatflow，按消息顺序上传可用图片/文件，失败不回退 MCP。

### 个人微信

- 独立「会话草稿」工作台与「配置中心」：数据目录、项目、回复风格、模型与 Skill / 只读 MCP、导出高级设置。
- 数据目录自动检测：扫描常见目录、校验 `session/session.db`，返回多个候选并评分；也可手动指定 `db_storage`。
- 会话项目绑定、消息与图片预览、项目导出。
- 草稿生成与自定义改写：按钮文案和提示词可在配置中心修改（默认更简短 / 更礼貌 / 更随意 / 换一个）。
- 只复制到剪贴板，或通过 Rust helper 填入微信空白输入框；**不点击发送、不模拟回车**。
- 支持一次性导入 `%APPDATA%\ProxyTalk` 的项目/消息/草稿/模型连接；原库只读。
- 个人微信数据保存在 `~/.wecom-issue-radar/wechat.sqlite3`，凭据用 DPAPI 保存，微信原始数据库只读。

### 沟通助手

- **回复助手**：潜台词与情绪解读、稳重/亲和/简短三版可编辑、复制/填入微信、发送前语气风险与改写建议。
- **话术库**：请假、催进度、婉拒、道歉、汇报等分类；支持 `{{姓名}}`、`{{日期}}`、`{{金额}}` 等变量；复制或填入微信；话术保存在本机 `phrases.json`。
- **长文速读**：粘贴长聊天记录、公告、邮件或会议记录，输出摘要 / 要点 / 风险 / 下一步；可编辑、复制或导出 Markdown。
- 模型未配置或调用失败时返回规则版结果并标记 `fallback`，不阻塞使用。

### 策略与发布

- **提示词库**：按问题分析、群回复、回复助手三个场景维护提示词；问题分析可定义独立字段、类型、枚举和默认值。
- **发布目标**：Excel / Markdown / 腾讯文档（预留飞书）。腾讯文档支持连接、字段映射、预览三 Tab：
  - 源字段来自当前问题分析提示词的字段 + 系统变量（日期、图片、发送人、消息时间、问题标识）；
  - 每个目标列可配置直接写入、固定值、模板拼接、日期格式化、枚举映射；
  - 支持默认值、必填校验、目标字段 Schema 增删改、旧模板导入；
  - 切换提示词时保留可匹配映射，失效来源标黄提示。
- 字段转换在前端预览与 Python 管线中执行同一套语义，保存时同时写回 `publish_targets` 与 `smart_sheet` 镜像。
- MCP 服务与 Dify 应用为独立接入页；监听器只消费其能力，不再维护逐工具授权。

### 设置

- 常用：企业微信数据目录、大模型、导出位置、界面大小、更新与诊断。
- 高级：OCR、腾讯文档上传凭据、Agent 日志、配置备份/导入等。
- 配置备份不包含本机目录、模型/OCR API Key、Webhook、MCP 密钥、企业微信凭据或访问令牌。

## 导出结果

### Excel

每个群生成一个 `.xlsx`，包含：

1. `导出说明`：处理日期、群名、生成时间、字段说明。
2. `聊天记录`：消息时间、发送人、消息类型、原文、截图 OCR、附件路径、消息 ID。
3. `问题清单`：按本次问题字段快照动态生成列，并保留消息时间、发送人、图片引用和问题 Key。

### Markdown

每个群可生成 `.md`，按时间保留聊天上下文、截图 OCR 和结构化问题清单，适合归档或交给其他模型继续分析。跨天、非全天范围会在文件名和内容中注明实际时间区间。

### 腾讯文档 Smart Sheet

同步的是大模型生成的结构化问题记录。同步前会校验目标模板、字段映射、必填项、类型与枚举；图片上传需要企业微信 Corp ID 和 Corp Secret。默认人工确认；定时任务可显式开启自动同步。去重台账以腾讯返回的 `record_id` 为依据，但外部写入不是严格的 exactly-once 保证，中断后应先核对远端文档。

## 使用流程

1. 安装并启动应用。
2. 在「设置 → 常用」自动检测企业微信数据目录和导出目录；检测不到时手动选择。
3. 企业微信运行中点击「提取密钥」。
4. 按需配置大模型、OCR、腾讯文档模板与字段映射。
5. 如需群内自动拟稿：先在「MCP 服务」或「Dify 接入」登记能力，再到「群监听回复」创建 Agent V2 监听器。
6. 在「提示词库」维护业务分析规则；在「发布目标」配置腾讯文档映射。
7. 回到「问题雷达」选择时间与群聊，执行导出。
8. 个人微信：在「配置中心」检测目录、建项目、调回复风格，然后回「会话草稿」生成与填入。
9. 沟通助手可直接使用：回复、话术、长文速读均不需要先配置聊天数据。

## 键盘快捷键

| 快捷键 | 作用 |
| --- | --- |
| `Ctrl/Cmd + K` | 快速跳转到任意页面/功能 |
| `Ctrl/Cmd + S` | 在提示词库保存当前提示词 |
| `Ctrl/Cmd + Enter` | 在回复助手/长文速读生成结果 |
| `Esc` | 关闭最上层弹窗/抽屉 |
| `Tab` / `Shift + Tab` | 弹层内循环焦点；关闭后焦点回到触发位置 |

## 本地数据与安全

配置与运行数据默认位于：

```text
%USERPROFILE%\.wecom-issue-radar\
├─ config.local.json          # 业务配置（模型/OCR/提示词/发布目标等）
├─ reply-runtime.sqlite3      # 群监听运行时数据库
├─ wechat.sqlite3             # 个人微信项目、消息与草稿
├─ phrases.json               # 沟通助手话术库
├─ schedule-state.json        # 定时任务状态
└─ logs\                      # 诊断日志（默认关闭详细 Agent 日志）
```

- 聊天数据、截图、配置和导出结果默认只在本机；只有主动点击生成/分析时才调用已配置的模型。
- 个人微信只读原始数据库；填入通过本机 Rust helper 完成，只填空白输入框，不发送。
- MCP 仅暴露只读工具；写/破坏性工具不进入模型可用列表，也不需要人工确认。
- 模型 API Key、Webhook、MCP Headers/Env、腾讯凭据等不会写入配置备份。
- 诊断日志会自动遮蔽 API Key、Token、Authorization、Webhook 和 MCP 密钥，但问题文本和工具结果仍可能含业务敏感信息，请只在排查时开启。

## 本地开发

前置：Windows 10/11、Node.js 22+、pnpm 10.13+、Python 3.11+；仅构建微信填入 helper 时需要 Rust 1.97+ 与 `x86_64-pc-windows-msvc` target。

```powershell
pnpm install --frozen-lockfile

# 仅启动渲染层（浏览器调试，不含 Electron 外壳、sidecar 和 bridge）
pnpm dev

# 构建 Electron main/preload 并用 Electron 启动（开发模式需要 .venv 里的 Python）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
pnpm dev:electron
```

构建微信填入 helper：

```powershell
rustup target add x86_64-pc-windows-msvc
cargo build --release --manifest-path native\wechat-input-helper\Cargo.toml
```

## 验证

```powershell
pnpm typecheck
pnpm test
pnpm build:renderer
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
cargo fmt --manifest-path native\wechat-input-helper\Cargo.toml --check
cargo clippy --manifest-path native\wechat-input-helper\Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path native\wechat-input-helper\Cargo.toml
```

## 打包与发布

不要在开发机执行 PyInstaller、EXE/NSIS 构建或安装包自检。sidecar、安装包、签名和安装级冒烟验证统一交给 GitHub Actions 的 Windows Runner。

- **main 推送**：Actions 运行测试、PyInstaller sidecar、Rust helper、Electron 构建，上传安装包 Artifact（默认保留 14 天）。
- **tag `vX.Y.Z`**：校验 `package.json` 版本与 tag 一致，构建 NSIS 安装包、`latest.yml`、blockmap 和兼容旧 Tauri 的 `latest.json`，创建并发布 GitHub Release。
- 版本单源：`package.json`；不要再修改 `src-tauri/**` 中的版本。正式发布见 [docs/release.md](docs/release.md)。

## 架构

```text
React 18 / TypeScript / Vite 渲染层
        │ preload 暴露的 IPC bridge
        ▼
Electron 45 主进程
  ├─ 配置、窗口、缩放、单实例、更新
  ├─ 任务调度与执行历史
  ├─ Worker 进程管理（--request / --reply-runtime / --extract-keys）
  ├─ ReplyRuntime 监督与事件转发
  └─ Rust wechat-input-helper 调用（只填入、不发送）
        ▼
Python sidecar（PyInstaller）
  ├─ 企业微信数据库解密、图片解析、导出
  ├─ OCR / 大模型分析 / 腾讯文档预览与写入
  ├─ 个人微信只读、草稿与导出
  ├─ 群监听 Agent V2 / Dify / 证据复核
  └─ 沟通助手：回复 / 话术 / 长文速读
```

## 技术栈

- Electron 45 / React 18 / TypeScript / Vite
- Tailwind CSS / lucide-react / Sonner
- Python / PyCryptodome / openpyxl / PyInstaller
- Rust / uiautomation（微信填入 helper）

## 相关文档

- [Agent Runtime v2](docs/agent-runtime-v2.md)：群监听执行模型、工具安全、恢复与诊断。
- [Electron 迁移说明](docs/electron-migration.md)：Tauri → Electron 的目录、命令、数据与升级路径。
- [发布流程](docs/release.md)：CI、tag、Release 资产与自动更新校验。
- [Dify Chatflow 接入](docs/dify-chatflow-api.md)
- [腾讯文档图片字段](docs/tencent-smartsheet-image-fields.md)

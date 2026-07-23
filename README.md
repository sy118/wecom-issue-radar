# 企微问题雷达

一个面向业务、产品和客户成功团队的 Windows 桌面工具：从本机企业微信群聊中提取指定日期的聊天记录和截图，通过可选的 OCR 与大模型分析整理问题，并导出 Excel、Markdown，或在二次确认后同步到腾讯文档 Smart Sheet。

当前桌面版使用 **Tauri 2 + Rust + React + TypeScript**，界面参考 cc-switch 的紧凑侧边导航、浅色/深色主题和玻璃卡片风格。聊天解密、OCR、模型调用和报表导出封装在随安装包分发的 Python sidecar 中，业务人员无需安装 Python、Node.js 或 Rust。

## 功能

- 自动检测企业微信是否运行、安装路径和数据目录
- 手动配置企业微信 `Data` 目录、数据库密钥和导出目录
- 读取本地数据库中的群聊列表，按日期和群处理
- 独立配置大模型与截图 OCR，包括 Base URL、API Key、模型和并发数
- 内置多套分析提示词，可新增、复制、编辑、删除并指定默认提示词
- 可以只导出原始聊天，也可以启用 OCR 和大模型问题提炼
- 同时支持 Excel、Markdown 和腾讯文档 Smart Sheet
- Smart Sheet 写入前显示预览并二次确认，本地台账避免重复写入
- 浅色/深色主题、无边框窗口和现代化任务进度界面

## 导出结果是什么

### Excel（推荐给业务人员）

每次任务生成一个 `.xlsx` 工作簿，包含三个 Sheet：

1. `导出说明`：处理日期、群名、生成时间和字段说明。
2. `聊天记录`：完整消息时间、发送人、消息类型、原文、截图 OCR、附件路径和消息 ID。
3. `问题清单`：大模型合并后的模块、问题分类、问题描述、总结、原因/结论、时间线和图片引用。

Excel 适合筛选、排序、二次补充和交给业务团队流转。即使不配置大模型，也可以导出完整聊天记录。

### Markdown（推荐用于归档或交给其他 AI）

每次任务同时可生成一个 `.md` 文件，按时间保留完整聊天上下文、截图 OCR 和结构化问题清单。它适合 Git/知识库归档、全文搜索，或者手动上传给其他大模型继续分析。

### 腾讯文档 Smart Sheet（可选）

Smart Sheet 同步的是大模型生成的结构化问题记录，而不是把整段聊天原样塞进表格。字段依据 `config.example.json` 中的 Schema 映射，默认包括：

- 模块、问题描述、原因、问题截图
- 复盘结论、处理状态、问题分类、典型案例
- 登记日期、问题总结、起止时间、Jira 链接等

同步依赖腾讯侧可接收 `records` 的 Webhook；图片上传还需要企业微信 `Corp ID` 和应用 `Corp Secret`。未配置 Smart Sheet 时，Excel 和 Markdown 完全不受影响。

## 使用流程

1. 安装并启动桌面应用。
2. 在“设置 → 企业微信与目录”中运行自动检测，确认数据目录和导出目录。
3. 首次使用时，在企业微信正在运行的情况下点击“提取密钥”。
4. 按需配置大模型、OCR 和腾讯文档。
5. 在“提示词”中选择或创建业务分析规则，并设为默认。
6. 回到“开始处理”，选择日期与群聊，再选择 OCR、分析和导出方式。
7. 任务结束后直接打开 Excel、Markdown 或完整任务目录。
8. 如果启用了 Smart Sheet，核对待写入数量后再次确认。

## 本地开发

### 环境

- Windows 10/11
- Node.js 22+
- pnpm 10+
- Rust stable，包含 MSVC Windows target
- Python 3.11+

### 安装与运行

```powershell
pnpm install
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
pnpm tauri:dev
```

开发模式下 Rust 会调用 `.venv\Scripts\python.exe worker\main.py`。发布版本则调用 Tauri 安装包内的 `wecom-issue-radar-worker.exe`。

### 验证

```powershell
pnpm typecheck
pnpm build:renderer
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
cargo fmt --manifest-path src-tauri\Cargo.toml --check
cargo clippy --manifest-path src-tauri\Cargo.toml --all-targets -- -D warnings
```

## Windows 打包

Python 处理引擎必须先构建为 Tauri sidecar：

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\pyinstaller.exe --clean --noconfirm worker\issue_radar_worker.spec
New-Item -ItemType Directory -Force src-tauri\binaries
Copy-Item dist\wecom-issue-radar-worker.exe src-tauri\binaries\wecom-issue-radar-worker-x86_64-pc-windows-msvc.exe
pnpm tauri build --target x86_64-pc-windows-msvc
```

安装包位于：

- `src-tauri/target/x86_64-pc-windows-msvc/release/bundle/nsis/*.exe`
- `src-tauri/target/x86_64-pc-windows-msvc/release/bundle/msi/*.msi`

## GitHub Actions

- `CI`：运行前端类型检查与构建、Python 测试、Rust 格式检查、Clippy 和测试。
- `Build Windows installers`：每次推送到 `main` 或手动运行时构建 NSIS EXE 与 MSI，并上传为 Actions Artifact。
- 推送 `v*` 标签时，同一工作流会创建 GitHub Release 并附加两个安装包。

在仓库的 **Actions → Build Windows installers → Run workflow** 中可以随时构建，无需本机安装 Rust。

## 架构

```text
React / TypeScript UI
        │ 少量 Tauri commands + progress events
        ▼
Rust desktop shell
  ├─ 本地配置与窗口能力
  ├─ 任务调度、路径打开
  └─ JSON 请求 / JSONL 进度协议
        ▼
Python pipeline sidecar
  ├─ 企业微信数据库解密与附件提取
  ├─ OCR 与大模型分析
  ├─ Excel / Markdown 导出
  └─ Smart Sheet 预览与写入
```

这条边界让 UI 保持轻量，也便于以后逐步把性能敏感模块迁移到 Rust，而不改变前端接口。

## 配置与安全

应用配置默认保存在：

```text
%USERPROFILE%\.wecom-issue-radar\config.local.json
```

以下内容不会提交到仓库：本地配置、企业微信数据库密钥、聊天缓存、附件、导出结果、模型 API Key 和腾讯凭据。

请勿把真实聊天数据或密钥加入 Issue。若任何 API Key、Webhook、`Corp Secret` 曾被提交到公开仓库，请立即在对应平台轮换。

## 技术栈

- Tauri 2 / Rust
- React 18 / TypeScript / Vite
- Tailwind CSS / lucide-react / Sonner
- Python / PyCryptodome / openpyxl / PyInstaller

# Electron 迁移说明（v4.0 → v4.2）

本文记录桌面壳从 **Tauri 2 + Rust** 迁移到 **Electron 45** 的架构与命令。业务能力、Python sidecar 与用户数据保持不变；v4.0 完成换壳与个人微信填入 helper，v4.1 重做界面，v4.2 删除 Legacy/聊天检索/周报/待办并新增沟通助手。回滚方式见文末。

## 架构

```text
Electron main (dist-electron/main.cjs)
  ├─ 窗口 / 单实例 / 安全基线（contextIsolation、sandbox、CSP）
  ├─ IPC 白名单  ← preload (dist-electron/preload.cjs) 经 contextBridge 暴露
  ├─ 调度器、配置外壳、进程管理（TypeScript）
  ├─ electron-updater（GitHub provider，读取 Release 的 latest.yml）
  ├─ 启动 Python sidecar
  │     ├─ 打包后：resources/bin/wecom-issue-radar-worker.exe（PyInstaller onefile）
  │     └─ 开发时：.venv\Scripts\python.exe worker/main.py
  └─ 调用 Rust 微信填入 helper（stdin/stdout 单行 JSON）
        ├─ 打包后：resources/bin/wechat-input-helper.exe（Rust 2021 + uiautomation）
        └─ 开发时：native/wechat-input-helper/target/x86_64-pc-windows-msvc/release/wechat-input-helper.exe

Renderer（React + Vite，构建产物 frontend-dist/，file:// 加载）
```

- **Electron main / preload**：由 esbuild 打包成 CJS（`scripts/build-electron.mjs`），入口是 `electron/main/index.ts` 与 `electron/preload/index.ts`，输出 `dist-electron/main.cjs`、`dist-electron/preload.cjs`；`package.json` 的 `main` 指向 `dist-electron/main.cjs`。
- **渲染层**：沿用 React + Vite，产物在 `frontend-dist/`，由主进程以 `file://` 加载。
- **单 Python sidecar**：PyInstaller onefile 构建，CI 产出 `dist/wecom-issue-radar-worker.exe`，electron-builder 通过 `extraResources` 放进安装目录的 `resources/bin/`。启动模式：`--request`（一次性任务）、`--reply-runtime`（群监听常驻运行时）、`--wechat-runtime`（个人微信常驻 worker）、`--extract-keys`（提取企微密钥），另有 `--self-check` 供 CI 冒烟。
- **个人微信常驻 worker（v4.4）**：首个个人微信动作时懒启动 `--wechat-runtime`，通过 NDJSON（`{id, action, payload}` → `{id, ok, data|error}`）复用 `get_runtime()`、数据库/联系人/轮询缓存；Electron `wechatRuntime` supervisor 负责请求关联、超时、崩溃重启和退出清理，启动失败时自动回退一次性 `--request`。首屏改用一次 `bootstrap` 动作返回 detect/projects/settings/providers。
- **Rust 微信填入 helper（M2）**：`native/wechat-input-helper`（Rust 2021 + uiautomation 0.25）编译为 `wechat-input-helper.exe`；主进程通过 stdin 写入单行 JSON，从 stdout 读取结果。helper 内部保留 8 秒的微信主窗口/聊天标题/焦点/空输入框校验链，只填入不发送。打包后从 `resources/bin/wechat-input-helper.exe` 启动；开发时从 `native/wechat-input-helper/target/x86_64-pc-windows-msvc/release/` 加载，也可用 `WECHAT_INPUT_HELPER` 覆盖。helper 缺失、启动失败或校验超时时，主进程返回可读错误，产品路径降级为“仅复制”，不会自动发送消息。
- **业务数据根不变**：`%USERPROFILE%\.wecom-issue-radar`（即 `~/.wecom-issue-radar`）。配置、数据库、调度状态、日志都在这里：
  - `config.local.json`：业务配置（模型 / OCR / 提示词 / 发布目标 / 个人微信设置；敏感字段按既有策略处理）
  - `reply-runtime.sqlite3`：群监听回复运行时数据库
  - `wechat.sqlite3`：个人微信项目、消息、草稿与模型连接
  - `phrases.json`：沟通助手话术库
  - `schedule-state.json`：定时任务状态
  - `logs/`、`logs/agent/`：运行日志
  - v4.2 起不再创建 `chat_search_index.db`；旧文件不再被读取，可手动删除。
- **userData 隔离**：Electron/Chromium 自己的缓存与本地状态位于 `%APPDATA%\<应用名>`（由 Electron 管理），不存放业务数据。业务数据路径只由 `~/.wecom-issue-radar` 解析（可用 `WECOM_ISSUE_RADAR_HOME` 覆盖，仅用于测试/便携场景）。
- **旧配置直接可用**：v3.5.3 的 `config.local.json`、`reply-runtime.sqlite3`、`schedule-state.json` 不需要迁移即可被 v4 读取；升级安装不会覆盖或清空数据根。v4.2 会在加载时清理不再支持的聊天检索/周报/待办数据，个人微信数据、话术、监听历史与配置保留。

## v4.2 主要变化

- 群监听仅 Agent V2；Legacy 运行时删除，旧监听器保存时自动归一为 v2。
- MCP 工具逐项人工授权删除：只读工具自动可用，写/破坏性工具不暴露；`mcp.tool_policy` 命令不再存在。
- 删除聊天查找、周报、待办及 `chat_search_index.db` 创建逻辑。
- 新增沟通助手（回复助手 / 话术库 / 长文速读），话术存放在 `phrases.json`。
- 个人微信新增配置中心、目录自动检测、自定义回复风格。
- 腾讯文档发布目标新增字段映射/转换参数，前后端共用转换语义。
- 统一 Portal 弹层，修复嵌套框架与缩放裁切。

## 开发命令

前置：Node 22+、pnpm 10.13.1、Python 3.12（仅运行 sidecar 时需要）、Rust 1.97.1（仅构建微信填入 helper 时需要，含 `x86_64-pc-windows-msvc` target）。本机不能构建安装包，打包一律在 CI。

```powershell
# 安装依赖（pnpm 10 的构建脚本白名单写在 pnpm-workspace.yaml 的 onlyBuiltDependencies）
pnpm install --frozen-lockfile

# 仅启动渲染层（浏览器调试，不含 Electron 外壳与 sidecar）
pnpm dev

# 构建 Electron main/preload 并用 Electron 启动（开发模式仍需要 .venv 里的 Python）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
pnpm dev:electron

# 构建微信填入 helper（开发时主进程从 native/.../target 加载）
rustup target add x86_64-pc-windows-msvc
cargo build --release --manifest-path native/wechat-input-helper/Cargo.toml --target x86_64-pc-windows-msvc

# 质量门禁
pnpm test        # 前端单测（vitest）
pnpm typecheck   # 渲染层 + Electron 源码的 tsc -b
python -m unittest discover -s tests -v
```

包管理器与 Node 版本以 `package.json` 的 `packageManager` 和 CI（Node 22）为准；本机 Node 24 仅用于日常开发。

## 构建命令

```powershell
pnpm build            # tsc -b + vite build + node scripts/build-electron.mjs
pnpm build:renderer   # 只构建渲染层到 frontend-dist/
pnpm build:electron   # 只构建 main/preload 到 dist-electron/
```

完整安装包（NSIS）需要 Python 3.12 + PyInstaller 6.21.0、Rust 1.97.1（含 `x86_64-pc-windows-msvc` target），且只能在 Windows 上打包：

```powershell
# 1) 先构建 Python sidecar（与 CI 相同）
pyinstaller --clean --noconfirm worker\issue_radar_worker.spec
dist\wecom-issue-radar-worker.exe --self-check

# 2) 构建 Rust 微信填入 helper 并复制到 dist/（与 CI 相同）
cargo fmt --manifest-path native/wechat-input-helper/Cargo.toml --check
cargo clippy --manifest-path native/wechat-input-helper/Cargo.toml --target x86_64-pc-windows-msvc --all-targets -- -D warnings
cargo test --manifest-path native/wechat-input-helper/Cargo.toml --target x86_64-pc-windows-msvc
cargo build --release --manifest-path native/wechat-input-helper/Cargo.toml --target x86_64-pc-windows-msvc
Copy-Item native\wechat-input-helper\target\x86_64-pc-windows-msvc\release\wechat-input-helper.exe dist\wechat-input-helper.exe -Force

# 3) 再打包（等价于 pnpm build + electron-builder）
pnpm package
# 或分步（CI 采用这种，避免重复 build）：
pnpm build
pnpm exec electron-builder --win nsis --publish never
```

产物在 `release/`：

- `wecom-issue-radar-<version>-setup.exe`：NSIS 安装包（未签名），`resources/bin/` 内同时包含 `wecom-issue-radar-worker.exe`（Python sidecar）与 `wechat-input-helper.exe`（Rust helper）
- `latest.yml`：electron-updater 更新清单
- `wecom-issue-radar-<version>-setup.exe.blockmap`：差分更新数据
- 正式 Release 还会额外带一个固定指向 v3.5.3 的兼容 `latest.json`（由 CI 生成，供旧 Tauri 客户端读取）

日常开发机（没有 Python/Rust/打包环境）只跑 `pnpm build:renderer`、`pnpm build:electron`、`pnpm test`、`pnpm typecheck`；安装包由 `.github/workflows/build-windows.yml` 在 `windows-latest` 上产出。发布流程见 [release.md](release.md)。

## 回滚步骤（Electron → Tauri）

Electron 的回滚只涉及桌面外壳，**数据不动**：

1. 从 [v3.5.3 Release](https://github.com/sy118/wecom-issue-radar/releases/tag/v3.5.3) 下载 `_3.5.3_x64-setup.exe`（或 MSI）。
2. 在 Windows「设置 → 应用」中卸载 v4（Electron NSIS 安装包；卸载不会删除 `~/.wecom-issue-radar`）。
3. 安装 v3.5.3，启动后直接读取原配置、运行时数据库与调度状态。
4. 若需阻止旧客户端继续提示更新：v3.5.3 客户端读取的 `latest.json` 固定指向 v3.5.3，本身不会再提示更高版本；不要删除该文件。

回滚期间建议把产生问题的 v4 Release 标记为预发布（或删除），避免其他用户继续自动更新到该版本；不要为此把 `latest.yml` 指回旧版本（electron-updater 不做版本降级）。

## 旧 Tauri 用户迁移说明

旧 Tauri 版本（≤ v3.5.3）不能原地升级为 Electron 安装包，需要手动迁移一次：

1. **卸载旧版**：Windows「设置 → 应用 → 企微问题雷达」卸载 v3.x。卸载只影响程序目录，业务数据在 `%USERPROFILE%\.wecom-issue-radar`，不会被删除。
2. **下载新版**：从 [Releases](https://github.com/sy118/wecom-issue-radar/releases) 下载最新的 `wecom-issue-radar-<version>-setup.exe`。
3. **安装并启动**：若出现 Windows SmartScreen 提示，选择「更多信息 → 仍要运行」（安装包暂未做 Authenticode 签名）。
4. **确认数据**：启动后配置、群列表、定时任务、回复运行时数据、日志都应直接可见，无需重新配置。若此前把导出目录放在程序安装目录，请把导出目录改到用户目录（数据根或自定义路径）。
5. **旧客户端的自动更新**：v3.x 客户端检查更新时读取的 `latest.json` 被固定在 v3.5.3，因此 v3.5.3 用户看到“已是最新”，更旧的 v3.x 用户最多升级到 v3.5.3，不会误装 Electron 包。

建议迁移顺序：先保留旧版安装包一段时间，确认 v4.2 功能回归通过后再删除；数据根建议在迁移前手工复制一份备份（如 `%USERPROFILE%\.wecom-issue-radar` → 备份目录）。

## 已知限制（v4.2）

- **仅 NSIS**：不再提供 MSI；需要 MSI/组策略批量分发的环境暂不支持。
- **安装包变大**：Electron 运行时 + Python sidecar，安装包约 150MB，首启比 Tauri 版稍慢。
- **未签名**：未购买 Authenticode 证书时会有 SmartScreen 提示；与 v3.5.3 现状一致，可在后续购买证书后通过 CI 的 `CSC_LINK` / `CSC_KEY_PASSWORD` 启用签名。
- **微信填入 helper 的边界**：仅 Windows；依赖微信窗口的 UIAutomation 结构（微信大版本变化可能失效）；helper 在 8 秒内无法确认目标聊天、焦点与空输入框时拒绝填入；helper 缺失、启动失败或超时时主进程报错并降级为“只复制”，绝不自动发送。
- **自动更新不降级**：electron-updater 只按 `latest.yml` 的更高版本更新；回退版本需要发布一个更高的修复版本，或让用户手动重装旧安装包。
- **旧 Tauri 无法原地升级**：必须按上一节手动卸载/安装一次。
- **只支持 Windows x64**：当前 CI 只产出 Windows x64 安装包。
- **构建只能在 CI**：本机没有完整的打包环境，`release/` 产物以 GitHub Actions 为准。
- **腾讯文档目标结构**：目标字段 Schema 目前以手动维护/旧模板导入为主，没有从腾讯文档自动读取表结构的 API；保存前后端均会校验映射与转换。
- **群监听只读工具**：只自动暴露 MCP 只读工具；写/破坏性工具不可用，也不提供人工放行。
- **已删除功能**：聊天查找、周报总结、待办与承诺在 v4.2 移除，旧索引文件不再读取。

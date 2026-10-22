# 发布流程（Electron / NSIS）

发布流水线在 `.github/workflows/build-windows.yml`，全部构建在 GitHub Actions 的 `windows-latest` 上完成；本机没有完整的 Python/PyInstaller/Rust/打包环境，**不要**用本机产物作为发布依据。

## 版本号规则（单一来源）

**`package.json` 的 `version` 是唯一版本号来源**。`electron-builder.yml`、Electron main、`latest.yml`、安装包文件名都以它为准。

- 发布 tag 必须严格匹配 `^v(\d+)\.(\d+)\.(\d+)$`（例如 `v4.2.0`），且 `v` 后的版本必须与 `package.json` 的 `version` 完全一致，否则 CI 在第一步失败。
- tag 指向的 commit 必须已包含在 `origin/main` 中（tag 打在 main 的历史提交上，不允许从侧分支直接发布）。
- 不再维护 Tauri/Cargo/`tauri.conf.json` 中的版本；原 `src-tauri/**` 不参与构建。

升级版本的标准动作：

1. 修改 `package.json` 的 `version`（例如 `4.2.1` → `4.2.2`），提交并合入 `main`。
2. 在 `main` 上打 tag `v4.2.2` 并 push：`git tag v4.2.2 && git push origin v4.2.2`。
3. 等待 `Build Windows installers` 工作流完成并发布 Release。

## 触发方式与产物

| 触发 | 结果 |
| --- | --- |
| `push` 到 `main` | 完整构建 + 校验，上传 artifact `wecom-issue-radar-windows-x64`（exe / `latest.yml` / blockmap，保留 14 天），不发布 Release |
| `push` tag `v*` | 完整构建 + 校验，创建 **draft** Release → 上传资产与兼容 `latest.json` → 发布 Release → 公网校验 |
| `workflow_dispatch` | 手动构建；非 tag 时只产出 artifact |

正式 Release 资产：

- `wecom-issue-radar-<version>-setup.exe`：NSIS 安装包（未签名）；安装包内 `resources/bin/` 同时包含 `wecom-issue-radar-worker.exe`（Python sidecar）与 `wechat-input-helper.exe`（Rust 微信填入 helper）
- `latest.yml`：electron-updater 更新清单（`version` / `path` / `sha512` / `releaseDate`）
- `wecom-issue-radar-<version>-setup.exe.blockmap`：差分更新数据
- `latest.json`：**固定指向最后一版 Tauri（v3.5.3）的兼容清单**，只给旧 Tauri 客户端读，内容不随新版本变化

安装后两个 native 组件都从安装目录的 `resources/bin/` 启动：Python sidecar（`--request` / `--reply-runtime` / `--extract-keys`）与 Rust 微信填入 helper（stdin/stdout 单行 JSON）。helper 失败时应用降级为“只复制”，不自动发送。

旧客户端读取地址：

- Tauri（v3.x）：`https://github.com/sy118/wecom-issue-radar/releases/latest/download/latest.json`
- Electron（v4+）：`https://github.com/sy118/wecom-issue-radar/releases/latest/download/latest.yml`

## CI 步骤

1. **Checkout**（`fetch-depth: 0`，保证 tag 校验能访问完整历史）。
2. **tag 校验**：tag 格式、tag 版本 == `package.json.version`、commit 包含在 `origin/main`。
3. 安装 pnpm 10.13.1、Node 22、Python 3.12、Rust 1.97.1（含 `rustfmt`/`clippy` 与 `x86_64-pc-windows-msvc` target）。
4. `pnpm install --frozen-lockfile`。
5. `pnpm test`、`pnpm typecheck`。
6. **Rust 微信填入 helper**（`native/wechat-input-helper`）：`cargo fmt --check` → `cargo clippy -D warnings` → `cargo test` → `cargo build --release`，再把 `wechat-input-helper.exe` 复制为 `dist/wechat-input-helper.exe`。
7. `python -m unittest discover -s tests -v`（含 `tests/test_release_metadata.py` 的发布元数据门禁）。
8. PyInstaller sidecar：`pyinstaller --clean --noconfirm worker\issue_radar_worker.spec`，运行 `dist\wecom-issue-radar-worker.exe --self-check`。
9. `pnpm build`：Vite 渲染层 + esbuild Electron main/preload，校验 `frontend-dist/index.html`、`dist-electron/main.cjs`、`dist-electron/preload.cjs` 与 `dist/` 下两个 native exe 存在。
10. `pnpm exec electron-builder --win nsis --publish never`（不重复 build）。
11. **产物校验**：`release/*.exe`、`release/latest.yml`、`release/*.blockmap` 存在；`latest.yml` 的 `version` == `package.json.version`、`path` 以 `.exe` 结尾、`sha512` 非空，且对应 exe/blockmap 存在；`release/win-unpacked/resources/bin/` 下两个 native exe 均在。
12. tag 时：校验仓库为 public（应用内更新需要匿名读取 Release），创建 draft Release，生成并上传 `latest.json`，发布 Release，最后从 `releases/latest/download/` 拉取 `latest.yml` 与 `latest.json` 并校验版本、签名、URL 与安装包可下载。

## Secrets / 环境变量

| 名称 | 用途 | 配置 |
| --- | --- | --- |
| `GITHUB_TOKEN` | 自动提供；创建 Release、上传资产、读取仓库可见性 | 无需配置（workflow 内 job 级 `contents: write`） |
| `LEGACY_TAURI_TAG` | 兼容 `latest.json` 指向的 Tauri 版本 | workflow 内固定 `v3.5.3`，修改需同步 `scripts/make-legacy-updater-manifest.mjs` 的默认值并评审 |
| `CSC_LINK` / `CSC_KEY_PASSWORD` | 可选：Authenticode 代码签名证书 | 未配置时保持未签名；配置后 electron-builder 会自动签名 |

不再需要 `TAURI_SIGNING_PRIVATE_KEY` / `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`（Tauri updater 已随 v3.5.3 停止更新）。

## 校验命令

发布完成后可在任意机器验证公网清单：

```powershell
# Electron 更新清单
curl.exe -L https://github.com/sy118/wecom-issue-radar/releases/latest/download/latest.yml

# 旧 Tauri 兼容清单（应始终是 3.5.3）
curl.exe -L https://github.com/sy118/wecom-issue-radar/releases/latest/download/latest.json
```

```powershell
# 断言版本与清单字段（在仓库根目录执行）
$version = (Get-Content -Raw package.json | ConvertFrom-Json).version
$latestYml = (curl.exe -sL https://github.com/sy118/wecom-issue-radar/releases/latest/download/latest.yml) -join "`n"
if ($latestYml -notmatch "(?m)^version:\s*$([regex]::Escape($version))\s*$") { throw "latest.yml 版本不符" }
if ($latestYml -notmatch "(?m)^sha512:\s*\S+") { throw "latest.yml 缺少 sha512" }

$legacyJson = (curl.exe -sL https://github.com/sy118/wecom-issue-radar/releases/latest/download/latest.json) -join "`n"
$legacy = $legacyJson | ConvertFrom-Json
if ($legacy.version -ne "3.5.3") { throw "兼容清单版本不是 3.5.3" }
if (-not $legacy.platforms.'windows-x86_64'.signature) { throw "兼容清单缺少签名" }
```

CI 在发布后会自动做同样的事情（30 次、每次 3 秒重试，覆盖 GitHub CDN 延迟）。

## 失败处理与重跑

- **tag 校验失败**：检查 tag 是否为 `vX.Y.Z`、是否与 `package.json.version` 一致、commit 是否在 `main` 上。修正后删除远端 tag 重打（`git push origin :refs/tags/vX.Y.Z`），或换一个正确的新版本号。
- **draft Release 已创建但后续步骤失败**：Release 仍是 draft，用户看不到也不会触发更新。修复后重跑工作流即可（上传步骤会覆盖同名资产）。
- **已发布 Release 需要撤下**：优先发布一个更高版本的修复包；必要时把问题 Release 标记为 prerelease 或删除，避免 `releases/latest` 指向它。
- **public 校验失败**：仓库必须是 public，应用内更新才能匿名读取 Release；若改为 private，需要把 `latest.yml` 与安装包迁到公开 CDN 并调整更新源。
- **兼容 `latest.json` 生成失败**：说明 v3.5.3 Release 的 `*.exe` / `*.exe.sig` 缺失或 API 不可用。该步骤会失败退出（不会静默生成空清单），修好后重跑。
- **安装包/blockmap 缺失**：检查 `release/` 是否被旧产物污染；必要时清理后重跑。CI 使用独立 runner，一般只需重跑。
- **Rust helper 门禁失败**：`cargo fmt --check`、`clippy -D warnings` 或 `cargo test` 失败会直接阻断构建；请 `native/wechat-input-helper` 作者在本地执行 `cargo fmt` 并修复 clippy 警告后重跑。不要放宽 `-D warnings`。
- **打包后缺 helper**：若 `release/win-unpacked/resources/bin/wechat-input-helper.exe` 缺失，检查 `dist/wechat-input-helper.exe` 是否已生成，以及 `electron-builder.yml` 的 `extraResources` 是否包含 helper。

## 回滚

### Electron 版本回滚

electron-updater 只按更高的 `version` 更新，**不能**把 `latest.yml` 指回旧版本。回滚方式是：

1. 发布一个版本号更高的修复包（例如问题版本 `4.0.1` → 回滚包 `4.0.2`，可包含回退后的代码）。
2. 紧急情况下让用户从 Releases 手动下载并重装旧版安装包；重装不会删除 `~/.wecom-issue-radar` 中的业务数据。

### 回退到 Tauri（M0 回滚）

1. 从 [v3.5.3](https://github.com/sy118/wecom-issue-radar/releases/tag/v3.5.3) 下载安装包。
2. 卸载 v4，安装 v3.5.3；业务数据在 `%USERPROFILE%\.wecom-issue-radar`，不受影响。
3. 保留 v3.5.3 Release 的 `latest.json`（兼容清单始终指向它），旧客户端不会报错。

详见 [electron-migration.md](electron-migration.md)。

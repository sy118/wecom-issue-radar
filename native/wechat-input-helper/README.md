# wechat-input-helper（个人微信“填入”Rust helper）

无 Tauri 依赖的独立 Windows 控制台小程序：从标准输入读取**一行 JSON**，用 Windows UI Automation
校验当前微信窗口/聊天标题/焦点/输入框状态，通过后把草稿填入个人微信输入框；向标准输出返回
**一行 JSON**。**只填入，绝不发送。**

本目录由 Electron 外壳以 `extraResources` 方式随包分发，Electron 侧负责 spawn、超时与杀进程树；
helper 自身不读命令行参数、不读写文件、不访问注册表/网络、不持久化任何状态。

## 安全边界

- 唯一写入动作是 `UIValuePattern::set_value`。**不点击发送按钮、不模拟回车、不注入键鼠、不读取消息内容。**
- 填入前必须同时通过以下校验（任一失败都不写入任何内容）：
  1. 个人微信主窗口唯一：`ControlType::Window` + class `Qt51514QWindowIcon` + name `微信`，8 秒窗口内；
  2. 焦点控件属于同一微信进程，且是该窗口的祖先链后代（≤32 层）；
  3. 控件类型为 `Edit` / `Document`，启用、非离屏；
  4. 控件矩形位于窗口消息编辑区（宽 ≥160、高 ≥36、左下区域、不越出窗口）；
  5. 目标聊天标题出现在窗口顶部标题栏区域；
  6. 输入框不是只读，且当前为空（避免覆盖用户已输入内容）；
  7. 写入前再次确认焦点未漂移、标题仍匹配，才调用 `set_value`。
- 空聊天标题 / 空草稿在初始化 UI Automation **之前**拒绝（单测覆盖，不触碰微信）。
- 错误原因固定为中文短句，不含草稿正文；草稿文本只交给 `set_value`，不落盘、不打印。

## 构建

要求：Windows x64 + Rust 1.97.1（MSVC target）。开发机无需安装 Rust，构建统一交给
GitHub Actions 的 Windows Runner。

```powershell
# 本地（已装 Rust）
cd native\wechat-input-helper
cargo test
cargo build --release --target x86_64-pc-windows-msvc
# 产物：native\wechat-input-helper\target\x86_64-pc-windows-msvc\release\wechat-input-helper.exe
```

> 本地开发若直接 `cargo build --release`，产物在 `target\release\`；Electron dev 会同时查找
> 上述两个目录。

```yaml
# CI（windows-latest）
- uses: dtolnay/rust-toolchain@1.97.1
  with:
    targets: x86_64-pc-windows-msvc
- run: cargo fmt --manifest-path native/wechat-input-helper/Cargo.toml --check
- run: cargo clippy --manifest-path native/wechat-input-helper/Cargo.toml --target x86_64-pc-windows-msvc --all-targets -- -D warnings
- run: cargo test --manifest-path native/wechat-input-helper/Cargo.toml --target x86_64-pc-windows-msvc
- run: cargo build --manifest-path native/wechat-input-helper/Cargo.toml --release --target x86_64-pc-windows-msvc
```

electron-builder 侧（由外壳任务修改 `electron-builder.yml`）：

```yaml
extraResources:
  - from: native/wechat-input-helper/target/x86_64-pc-windows-msvc/release/wechat-input-helper.exe
    to: bin/wechat-input-helper.exe
```

首次构建会在本目录生成 `Cargo.lock`，应一并提交以固定依赖版本。

## stdin / stdout 契约

stdin：**一行** UTF-8 JSON（读取到 `\n` 或 EOF 为止，调用方写完后可保持管道打开；容忍 BOM、CRLF 与未知字段；单行上限 1 MiB，含换行；多余行忽略）：

```json
{"chatTitle":"目标聊天","text":"草稿内容"}
```

stdout：**恰好一行** UTF-8 JSON，随后进程退出：

| 结果 | 退出码 | 输出 |
|---|---|---|
| 成功 | 0 | `{"filled":true,"chatTitle":"目标聊天","characters":4}` |
| 失败 | 1 | `{"filled":false,"error":"中文原因"}` |

- `characters` 为草稿的 Unicode 标量数量（`chars().count()`），不是字节数。
- stdin 为空、非 UTF-8、超过 1 MiB、非法 JSON、缺字段、标题/文本为空、窗口/焦点/标题/输入框校验失败、
  `set_value` 被拒绝、内部 panic，都会输出结构化失败 JSON 并退出码 1；**永远只有一行 stdout**。
- 超时：焦点确认循环 8 秒（沿用原 Tauri 命令），整体预算 12 秒；超时返回
  `{"filled":false,"error":"未在 8 秒内确认目标微信聊天和空白输入框，已拒绝填入；请使用复制。"}`
  或 `{"filled":false,"error":"填入超时（超过 12 秒），已中止且未发送任何消息；请使用复制。"}`。
- stderr 仅用于诊断，调用方应忽略；不要从 stderr 判断结果。
- 编码按管道 UTF-8 读写，不依赖 Windows 控制台代码页。

## Electron 调用示意

```ts
import { spawn } from "node:child_process";

const child = spawn(helperPath, [], {
  stdio: ["pipe", "pipe", "pipe"],
  windowsHide: true,
});

child.stdin.end(JSON.stringify({ chatTitle, text }) + "\n");
// 读取首行 stdout → JSON.parse；同时设置 15s 外部超时（helper 内部 12s 预算 + 启动余量），
// 超时或异常时用 taskkill /PID <pid> /T /F 杀进程树，并提示用户改用复制。
```

调用方应把 `filled:false` 的 `error` 原样展示，并保持草稿可复制；不要自动重试填入。

## 测试

`cargo test`（Windows 与非 Windows 均可跑）：

- 空标题 / 空文本在 UIA 初始化前被拒绝；
- 请求解析：BOM/CRLF/未知字段容忍，空输入、非法 JSON、非对象、缺字段、错类型拒绝；
- stdout 契约：成功/失败 JSON 的字段名、顺序与中文错误文本。

真机填入无法单测，需要在 Windows 上打开个人微信、选中目标聊天后手工验证；不通过时只能确认
“拒绝填入、不写入任何内容”，不得放宽校验。

## 已知限制

- 只支持**个人微信**（窗口 class/名称按当前版本固定）；企业微信不支持。
- 微信升级改动 Qt 控件树后校验会失败（安全失败，不会误填），需要重新核对选择器与校验。
- 若 UI Automation 的 COM 调用本身卡死，进程内无法强制中断；由 Electron 侧外部超时 + 杀进程树兜底。
- 未做 Authenticode 签名：首次运行可能触发 SmartScreen 或杀软提示；发布时建议与主程序统一签名。
  本 helper 无网络、无持久化、无文件写入，被误判的概率低于原生 Node 模块。

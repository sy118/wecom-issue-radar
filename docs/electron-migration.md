# Electron 迁移说明（v4.0 → v4.6）

本文记录桌面壳从 **Tauri 2 + Rust** 迁移到 **Electron 45** 的架构与命令。业务能力、Python sidecar 与用户数据保持不变；v4.0 完成换壳与个人微信填入 helper，v4.1 重做界面，v4.2 删除 Legacy/聊天检索/周报/待办并新增沟通助手，v4.3 优化个人微信会话性能，v4.4 引入个人微信常驻 worker，v4.5 收口模型单一事实来源并完成全应用企业级极简 UI 精修，v4.6 移除沟通助手并新增企业微信高情商回复。回滚方式见文末。

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
- **个人微信常驻 worker（v4.4）**：首个个人微信动作时懒启动 `--wechat-runtime`，通过 NDJSON（`{id, action, payload}` → `{id, ok, data|error}`）复用 `get_runtime()`、数据库/联系人/轮询缓存；Electron `wechatRuntime` supervisor 负责请求关联、超时、崩溃重启和退出清理，启动失败时自动回退一次性 `--request`。首屏改用一次 `bootstrap` 动作返回 detect/projects/settings/providers。v4.5 起每次启动与 bootstrap 都用全局 `config.llm` 对齐 `llm-default` 模型连接（保留已保存密钥与自定义 provider），配置中心只读展示全局模型。
- **Rust 微信填入 helper（M2）**：`native/wechat-input-helper`（Rust 2021 + uiautomation 0.25）编译为 `wechat-input-helper.exe`；主进程通过 stdin 写入单行 JSON，从 stdout 读取结果。helper 内部保留 8 秒的微信主窗口/聊天标题/焦点/空输入框校验链，只填入不发送。打包后从 `resources/bin/wechat-input-helper.exe` 启动；开发时从 `native/wechat-input-helper/target/x86_64-pc-windows-msvc/release/` 加载，也可用 `WECHAT_INPUT_HELPER` 覆盖。helper 缺失、启动失败或校验超时时，主进程返回可读错误，产品路径降级为“仅复制”，不会自动发送消息。
- **业务数据根不变**：`%USERPROFILE%\.wecom-issue-radar`（即 `~/.wecom-issue-radar`）。配置、数据库、调度状态、日志都在这里：
  - `config.local.json`：业务配置（模型 / OCR / 提示词 / 发布目标 / 个人微信设置；敏感字段按既有策略处理）
  - `reply-runtime.sqlite3`：群监听回复运行时数据库（v4.6.5 起也保存高情商回复的「我的会话」清单与本地别名；v4.6.6 起同表保存每群提示词绑定 `prompt_id` 与角色补充 `prompt_note`）
  - `wechat.sqlite3`：个人微信项目、消息、草稿与模型连接
  - `phrases.json`：历史遗留（原沟通助手话术库）。v4.6 起不再读取或写入，文件原样保留，可手动删除
  - `schedule-state.json`：定时任务状态
  - `cache/chat-ocr/`：历史遗留。v4.6 曾用于高情商回复的图片 OCR 缓存；v4.6.3 起本功能不再写入，目录可安全删除
  - `logs/`、`logs/agent/`：运行日志
  - v4.2 起不再创建 `chat_search_index.db`；旧文件不再被读取，可手动删除。
  - v4.6.4 起高情商回复的会话目录 / 上下文缓存与预热都在运行时内存中，不新增数据文件，也不改变只读语义。
- **userData 隔离**：Electron/Chromium 自己的缓存与本地状态位于 `%APPDATA%\<应用名>`（由 Electron 管理），不存放业务数据。业务数据路径只由 `~/.wecom-issue-radar` 解析（可用 `WECOM_ISSUE_RADAR_HOME` 覆盖，仅用于测试/便携场景）。
- **旧配置直接可用**：v3.5.3 的 `config.local.json`、`reply-runtime.sqlite3`、`schedule-state.json` 不需要迁移即可被 v4 读取；升级安装不会覆盖或清空数据根。v4.2 会在加载时清理不再支持的聊天检索/周报/待办数据；个人微信数据、监听历史与配置保留（`phrases.json` 自 v4.6 起不再读取，见下节）。

## v4.2 主要变化

- 群监听仅 Agent V2；Legacy 运行时删除，旧监听器保存时自动归一为 v2。
- MCP 工具逐项人工授权删除：只读工具自动可用，写/破坏性工具不暴露；`mcp.tool_policy` 命令不再存在。
- 删除聊天查找、周报、待办及 `chat_search_index.db` 创建逻辑。
- 新增沟通助手（回复助手 / 话术库 / 长文速读），话术存放在 `phrases.json`（v4.6 已移除，见下节）。
- 个人微信新增配置中心、目录自动检测、自定义回复风格。
- 腾讯文档发布目标新增字段映射/转换参数，前后端共用转换语义。
- 统一 Portal 弹层，修复嵌套框架与缩放裁切。

## v4.3–v4.5 主要变化

- **v4.3（个人微信会话性能）**：进入项目只增量读取游标之后的新消息；后台轮询 10s → 30s，离开页面暂停；图片预览限制并发并扩大缓存；引用消息按类型解码为可读占位；左侧项目行支持删除（只删本地项目、缓存、草稿与游标，不改微信原始数据库）。
- **v4.4（个人微信常驻 worker）**：新增 `--wechat-runtime` 常驻 worker，首个个人微信动作时懒启动；Electron supervisor 负责请求关联、超时、崩溃重启与退出清理，启动失败自动回退一次性 `--request`；首屏改用一次 `bootstrap`；配置中心显示 worker 模式、启动耗时与重启次数。
- **v4.5（模型单一事实来源 + UI 精修）**：个人微信 worker 每次启动/bootstrap 用全局 `config.llm` 校准 `llm-default` 模型连接（保留密钥与自定义 provider）；配置中心与「Agent v2 模型能力」卡片只展示当前生效模型，旧检测结果标注「上次检测（已过期）」。新增 `src/system.css` 语义 token 层，统一壳层、共享组件与全部页面的企业级极简视觉，补齐浅/深色对比度、键盘焦点与 reduced-motion。

## v4.6 主要变化

- **BREAKING：移除沟通助手**：删除回复助手 / 话术库 / 长文速读三页与侧栏、工作台入口，删除 `daily_action` IPC、`worker/daily`（summarize / phrases）与相关测试；提示词库 `eq_reply` 场景保留并把标签改为「高情商回复」。`phrases.json` 不再读取或写入，原样保留可手动删除；旧配置无需迁移。
- **新增企业微信 → 高情商回复**：`src/pages/wecom/HighEqReplyPage.tsx` 自动列出最近群聊与私聊（按最后消息时间排序），只读读取所选会话最近 30 条 / 6 小时上下文（v4.6.4 起改为固定视口 + 20 条分页，见下节），页面可见时每 30 秒轮询；默认选中最后一条消息，可点选切换或手动粘贴；生成潜台词解读、稳重/亲和/简短三版与发送前检查，仅复制不发送。
- **OCR 与缓存（v4.6.0 的加载时 OCR，v4.6.3 已移除）**：v4.6.0 在加载上下文时用独立 OCR 模型识别图片并缓存到 `<数据目录>/cache/chat-ocr`；v4.6.3 起改为生成时把图片交给全局模型，不再写入该缓存，详见下节。
- **后端与 IPC**：常驻回复运行时新增 `chat.recent` / `chat.context` / `chat.ocr` 只读查询（首次查询时自动启动 sidecar）；生成改为一次性 `assistant_action`（仅 `eq_reply`），后端模块为 `worker/reply_assistant`；`worker/issue_radar_worker.spec` 同步替换打包模块。

## v4.6.3 主要变化

- **图片改为生成时交给模型**：高情商回复加载上下文时不再 OCR、不再调用 `chat.ocr`；图片消息只显示「图片 ×N」与「生成回复时会把 N 张图片交给模型识别」。点击「生成高情商回复」时，前端把会话 id 与最多 6 个图片消息 id 交给 `assistant_action("eq_reply")`，后端 `worker/reply_assistant/images.py` 从企微 Cache/Image 解析可读图片（单张 ≤ 8 MB），随请求发给全局模型（OpenAI-compatible `image_url` 或 Anthropic `image` 内容块）。
- **图片回退与缓存**：模型不支持图片输入或带图片请求失败时，自动重试一次纯文本请求，仍失败才返回规则版（v4.6.6 起「规则版」改称「本地模板」，且仅模型明确不支持图片时才用剩余预算重试）；不再写 `<数据目录>/cache/chat-ocr`。高情商回复不再需要「设置 → 模型与 OCR」的独立 OCR 模型（该设置仍用于问题雷达等截图识别）。`chat.ocr` 查询仍保留在运行时，但高情商回复页面不再调用。
- **私聊名称修复**：私聊（`S:<自己>_<对方>`）优先解析对方名称；若企微存储的标题等于当前账号自己的名字则忽略，回退对方名称 / 成员备注，仍解析不到时显示「私聊会话」，不再把自己的名字显示成会话名。
- **最近会话排序**：`last_message_time` 兼容秒与毫秒并统一归一化为秒；后端多取候选后按（最后消息时间、最后消息 id）倒序重排并截断，列表副标题改为「群聊与私聊 · 按最后消息排序」。

## v4.6.4 主要变化

- **上下文固定视口与分页**：≥1280 宽度时工作区高度固定为 `clamp(480px, calc(100vh - 190px), 920px)`，会话列与上下文面板内部滚动、上下文头固定，页面不再整体滚动；≤1279 回退为两栏自动高度（回复面板下移）、≤900 单栏，窄布局下页面可滚动但上下文面板内部仍滚动。进入或切换会话默认加载最近 20 条（6 小时窗口，窗口内为空时回退最近 30 天并标记 `windowFallback`）并停在底部最新消息；向上滚到顶部（`scrollTop <= 24`）且 `hasMore` 时按每页 20 条加载更早消息，插入后按新增高度校正 `scrollTop` 保持阅读位置。新消息到达时距离底部 < 40px 自动跟随，否则显示「跳到最新」；长消息仅在超过 600 字符或 12 行时折叠（「展开全文 / 收起」），不再统一截 3 行。
- **会话名称解析**：直聊 `S:<自己 userid>_<对方 id>` 拆分自身与对方；自身身份用 `user_table.account`（其次 `id`）匹配，取不到时用「同一直聊标题在 ≥3 个会话重复出现」兜底判定为自己的名字。直聊取名顺序：对方名称（`user_map[peer]` → 成员昵称）→ 企微标题（仅当不等于自己名字）→ 重复标题兜底 → 「私聊会话」；群聊用 `roomname_remark or name`。`chat.recent` 返回补充 `selfSenderId` / `selfSenderName`（本轮仅透出，不在上下文里标记「我」）。
- **运行时缓存与预热**：`worker/reply_runtime/message_source.py` 增加目录缓存（会话行 + 成员昵称 + 用户映射，TTL 30 秒）与每会话上下文缓存（TTL 10 秒），`refresh=true` 绕过；`read_context` 复用目录缓存取名，不再二次解密 `session.db`。首次 `chat.recent` 成功后由后台 daemon 线程对最近会话执行 `warmup()` 读取消息快照，失败静默、不阻塞查询；缓存只在进程内存，不落盘、不改变只读语义。
- **前端缓存与刷新让路**：按会话缓存上下文（stale-while-revalidate），切回旧会话先渲染上次内容再静默刷新（静默刷新走运行时短缓存）；无缓存的首屏加载与手动刷新传 `refresh=true` 强制最新，30 秒轮询使用缓存参数。首屏 `CONTEXT_PAGE_SIZE = 20`，生成时只使用当前已加载的消息，向上加载更早页后再生成可让模型参考更多历史。
- **查询契约（向后兼容）**：`chat.recent` 新增可选 `refresh`，返回 `selfSenderId` / `selfSenderName`；`chat.context` 新增可选 `limit`（默认 20）、`before`（上页最早消息的 `sendTime` 秒）、`refresh`，返回补充 `windowFallback`、`hasMore`、`selfSenderId`，消息按 `sendTime` 升序；无 `before` 时读取 6 小时窗口最近 `limit` 条，有 `before` 时读取 `sendTime <= before` 的最近 `limit` 条（30 天窗口），`hasMore` 通过多取 1 条判断。
- **不变更**：图片仍在生成时交给模型、加载时不 OCR；不做「自己消息标记为我」；不引入跨进程持久缓存。

## v4.6.5 主要变化

- **「我的会话」清单改为用户维护**：左侧标题改为「我的会话」（副标题「仅显示已添加的会话」），不再自动列出全部最近会话，只展示用户显式添加的会话，未添加的会话不参与轮询与同步。运行时 SQLite 新增表 `reply_assistant_chats(conversation_id TEXT PRIMARY KEY, alias TEXT NOT NULL DEFAULT '', added_at REAL)`；通过 `replyRuntimeExecute` 的 `chat.chats.add`（body `{chats:[{conversationId, alias?}]}`，已存在则更新别名）、`chat.chats.rename`（`{conversationId, alias}`，空别名清除）、`chat.chats.remove`（`{conversationIds:[...]}`）维护，沿用 `commandId` + `expectedRevision` 的幂等与 revision 冲突重试，命令返回 `{revision}` 并失效运行时目录 / 上下文缓存；`conversationId` 校验 `^(R|S):`，别名去空白且最长 40 字符，数组去重，非法项报 `INVALID_CHAT`。
- **会话目录查询**：新增 `chat.catalog` 查询 `{limit?:300, search?:""}`，返回 `{chats:[{conversationId, displayName, alias, added, lastMessageTime, lastMessageId, isGroup}], revision, selfSenderId, selfSenderName}`；`limit` clamp 1..300，`search` 对名称 / 别名 / 会话 ID 做不区分大小写过滤（前端也可本地过滤），按最后消息时间降序；使用运行时目录缓存（30 秒 TTL，`refresh` 不影响此查询），供添加弹窗显示已添加状态。
- **`chat.recent` 只返回已添加会话**：从清单表取会话 ID，再用目录缓存补名称与最后消息时间，按（最后消息时间、最后消息 id）降序；返回 `{chats, revision, selfSenderId, selfSenderName}`，每条会话带 `alias` 与 `available`（目录中找不到时为 `false`，仍保留别名并提示「会话不可用」）；空清单返回空数组，前端显示引导添加的空态；会话清单为空时页面不自动选会话。
- **多消息综合回复**：前端用 `selected` 快照记录选中消息——私聊进入会话默认选中最后一条，群聊默认不选；点击消息 toggle 加选/取消，目标面板可单条移除或清空，切换会话清空并重新套用默认；生成 payload 中 `text` 为选中消息拼接（供规则回退），`focusMessages` 为 `[{senderName, timeLabel:"HH:mm", text}]`（当时最多 20 条，v4.6.6 起改为 10），`context` 排除所有选中消息，`imageMessageIds` 为上下文图片与选中消息图片的并集（当时最多 6 张，v4.6.6 起改为 3 张并新增图片开关）；`canGenerate` 在手动文本非空、或选中非空、或当前会话已有加载的上下文时成立（群聊默认 0 选中即依赖此项）。后端 `assistant_action("eq_reply")` 新增可选 `focusMessages`：存在时生成「需要回复的消息（共 N 条）」列表并提示综合回应、不要逐条机械回复；没有时保持原单条 `text` 逻辑；规则回退使用拼接文本，`text` 与 `focusMessages` 都为空仍报错。
- **兼容性与持久化**：`chat.recent` / `chat.context` 旧调用保持兼容；会话清单初始为空（页面提示添加），重启后保留；回滚到 v4.6.4 会恢复自动列表（运行时表保留但不使用）；别名与移除只影响本地清单，不写企微数据库、不删除聊天记录；「我的会话」清单暂不包含在配置备份中。

## v4.6.6 主要变化

- **每群提示词与角色补充**：`reply_assistant_chats` 新增 `prompt_id`、`prompt_note` 两列（旧库通过 `PRAGMA table_info` + `ALTER TABLE` 迁移，默认空串）；新增写命令 `chat.prompt.set`（body `{conversationId, promptId?, promptNote?}`，字段缺省表示不修改、空串表示清除，`promptNote` ≤500 字符，仅允许已添加会话，沿用 `commandId`/`expectedRevision` 幂等与冲突重试，返回 `{revision}`）；`chat.recent` / `chat.catalog` 返回 `promptId`、`promptNote`。
- **提示词解析**：按 `promptId` 精确匹配 `scene == eq_reply` 的条目，找不到时回退第一条 `eq_reply`，再回退内置默认；`promptNote` 以「本群角色补充：…」追加到系统提示词，不替换基础提示词，JSON 输出契约保持不变。结果 `ai` 返回 `{promptId, promptFallback, fallbackReason}`，提示词被删除时前端显示「提示词已删除，使用默认」。
- **生成预算**：总预算 45 秒；带图片第一次最多 22 秒，只有分类为 `vision_unsupported`（模型明确不支持图片/图片输入被拒绝）时才用剩余时间（保留至少 5 秒余量，最多 18 秒）重试纯文本；超时、网络错误、4xx/5xx、JSON 解析失败、空响应不再发起第二次长等待，直接回退本地模板；无图片单次最多 35 秒。`model.call_llm` 支持显式 `timeout_seconds`，回复助手不再受旧的 180 秒配置影响。
- **错误分类与结果字段**：`ModelUnavailable.kind` 支持 `timeout` / `http_4xx` / `http_5xx` / `network_error` / `invalid_response` / `empty` / `vision_unsupported`；`eq_reply` 返回 `timings{imageMs,modelMs,parseMs,totalMs,attempts}`、`ai{promptId,promptFallback,fallbackReason}`、`images{attached,skipped}`；`fallbackReason` 取值 `not_configured` / `timeout` / `vision_unsupported` / `http_error` / `network_error` / `invalid_response` / `empty` / `partial` / `unknown`，`partial` 表示模型返回部分章节、缺失部分由本地模板补齐。前端结果头部显示耗时与「AI 生成」/「本地模板」，按原因展示文案，不再统一提示“AI 暂时不可用”。
- **图片与上下文上限**：单轮最多 3 张、单张 ≤8MB、合计 ≤12MB，超出数量/大小的可用图片计入 `skipped` 并在结果中显示「忽略 N 张」；页面新增「带图片理解」开关（默认开，关闭时 payload 不包含图片 id）。`focusMessages` 上限从 20 改为 10（前后端一致），上下文拼接上限从 4000 改为 3000 字符，超出时提示「上下文已截断」。
- **前端入口**：`App.tsx` 向页面传入 `config`；生成面板新增「本群提示词」（默认条目 + 提示词库中所有 `eq_reply` 提示词）与「本群角色补充」（≤500 字，保存/清除），切换会话自动带出，并提供跳转「策略与发布 → 提示词库」的入口。
- **兼容性**：旧的 `eq_reply` 调用与既有结果字段保持兼容；提示词绑定与角色补充只存本机运行时 SQLite，不写企微数据库，配置备份暂不包含；回滚到 v4.6.5 后新增列保留但不使用。

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

# 2) 构建 Rust 微信填入 helper 并复制到 dist/（与 CI 相同；CI 用 cargo fmt 自动应用格式化）
cargo fmt --manifest-path native/wechat-input-helper/Cargo.toml
cargo clippy --manifest-path native/wechat-input-helper/Cargo.toml --target x86_64-pc-windows-msvc --all-targets
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

日常开发机只做源码级验证（`pnpm build:renderer`、`pnpm build:electron`、`pnpm test`、`pnpm typecheck`、Python 单测）；`ci.yml` 在 PR 与 `main` 推送时执行快速门禁，安装包只由 `.github/workflows/build-windows.yml` 在 `windows-latest` 上产出。发布流程见 [release.md](release.md)。

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

建议迁移顺序：先保留旧版安装包一段时间，确认 v4.6 功能回归通过后再删除；数据根建议在迁移前手工复制一份备份（如 `%USERPROFILE%\.wecom-issue-radar` → 备份目录）。

## 已知限制（v4.6）

- **仅 NSIS**：不再提供 MSI；需要 MSI/组策略批量分发的环境暂不支持。
- **安装包变大**：包含 Electron 运行时与 Python sidecar，安装包明显大于 Tauri 版，首启也稍慢。
- **未签名**：未购买 Authenticode 证书时会有 SmartScreen 提示；与 v3.5.3 现状一致，可在后续购买证书后通过 CI 的 `CSC_LINK` / `CSC_KEY_PASSWORD` 启用签名。
- **微信填入 helper 的边界**：仅 Windows；依赖微信窗口的 UIAutomation 结构（微信大版本变化可能失效）；helper 在 8 秒内无法确认目标聊天、焦点与空输入框时拒绝填入；helper 缺失、启动失败或超时时主进程报错并降级为“只复制”，绝不自动发送。
- **自动更新不降级**：electron-updater 只按 `latest.yml` 的更高版本更新；回退版本需要发布一个更高的修复版本，或让用户手动重装旧安装包。
- **旧 Tauri 无法原地升级**：必须按上一节手动卸载/安装一次。
- **只支持 Windows x64**：当前 CI 只产出 Windows x64 安装包。
- **打包只在 CI**：本机只做源码级验证，sidecar、NSIS、签名与安装级冒烟都在 GitHub Actions 完成，`release/` 产物以 CI 为准。
- **模型配置只有全局一份**：个人微信配置中心与群监听都不再单独维护模型，统一读取「设置 → 大模型」；修改后需保存设置，模型能力卡片需要重新检测才能反映新配置。
- **高情商回复依赖企业微信数据**：只有企业微信运行且数据目录、数据库密钥有效时才能读取「我的会话」目录与上下文；首次读取默认最近 6 小时，窗口内没有消息会回退最近 30 天，仍没有才显示空态，可切「手动输入」粘贴消息后生成。
- **「我的会话」清单与每群提示词是运行时数据**：清单（会话 ID、本地别名、每群提示词 ID、本群角色补充、添加时间）保存在 `reply-runtime.sqlite3`，重启保留，但配置备份暂不包含；换机 / 重装后需要重新添加会话并重设别名、提示词与角色补充。别名与提示词绑定只在本机生效，不写入企微数据库、也不修改企微备注 / 标星；移除只从清单移除，不删除聊天记录。
- **提示词删除会自动回退**：绑定的提示词在提示词库被删除后，该群回退使用第一条 `eq_reply` 提示词（或内置默认），结果中标记「提示词已删除，使用默认」；角色补充仍按本群保存内容生效，最多 500 字。
- **高情商回复有固定生成预算**：单次生成总预算 45 秒；带图片第一次最多 22 秒，只有模型明确不支持图片时才用剩余时间（最多 18 秒）重试纯文本；无图片单次最多 35 秒。超时、网络错误、返回解析失败等会直接回退本地模板，结果显示耗时与原因；慢模型可关闭「带图片理解」或缩短上下文/减少选中消息。
- **未添加的会话不同步**：高情商回复只展示并读取已添加会话；未添加的群聊 / 私聊不会出现在页面，也不会被轮询或读取（重新添加即可恢复）。
- **高情商回复的上下文是分页加载的**：默认只加载最近 20 条，向上滚动按需加载更早一页；生成时只使用当前已加载的消息，需要更早的上下文时先向上加载再生成。拼接后的上下文最多发送 3000 字符（超出会截断并提示），多选消息最多 10 条进入模型。
- **多选消息使用快照**：选中的消息以点选时的内容参与生成，刷新或加载更早分页后仍保留；切换会话会清空选择并重新套用默认（私聊最后一条、群聊不选）。
- **高情商回复的缓存是短时的**：运行时会话目录缓存约 30 秒、每会话上下文缓存约 10 秒，可能短暂显示旧数据；点「刷新」会跳过缓存强制获取最新，前端切回旧会话也会先显示缓存再静默刷新。
- **窄窗口布局不同**：工作区在 ≥1280 宽度下固定高度、页面不整体滚动；≤1279 回退为自动高度（回复面板下移），≤900 单栏，此时页面可滚动，但上下文面板内部仍保留滚动。
- **不在上下文标记「我」**：本轮只透出 `selfSenderId` / `selfSenderName`，自己发送的消息不会显示「我」标记，发送人名称保持原有显示。
- **高情商回复的图片在生成时才外发**：生成回复时会把上下文与选中消息里的图片随请求发给全局模型，实际最多 3 张、单张 ≤8MB、合计 ≤12MB（超出会忽略并在结果中显示忽略张数）；页面提供「带图片理解」开关（默认开，关闭时只发送文字）。加载会话时不识别图片，也不写本地 OCR 缓存。只有模型明确不支持图片时才用剩余预算重试纯文本，超时/网络等失败直接回退本地模板；无需为高情商回复单独配置 OCR 模型。
- **高情商回复仅复制**：不会发送消息、不会填入企微输入框、也不会模拟键盘，生成结果需由用户自行粘贴发送。
- **腾讯文档目标结构**：目标字段 Schema 目前以手动维护/旧模板导入为主，没有从腾讯文档自动读取表结构的 API；保存前后端均会校验映射与转换。
- **群监听只读工具**：只自动暴露 MCP 只读工具；写/破坏性工具不可用，也不提供人工放行。
- **已删除功能**：聊天查找、周报总结、待办与承诺在 v4.2 移除；沟通助手（回复助手 / 话术库 / 长文速读）在 v4.6 移除，`phrases.json` 不再读取，旧索引文件不再读取。

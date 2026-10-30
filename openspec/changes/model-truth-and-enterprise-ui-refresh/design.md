# Design

## Context

- 模型链路：全局配置存于 `config.local.json` 的 `llm`；个人微信 worker 在 `_ensure_default_provider()` 中仅在 `providers` 表为空时把 `llm` 复制成 `llm-default` 行（`worker/wechat/service.py`），而前端 `normalizeProviders()` 丢弃了 worker 已经返回的实时 `llm`（`worker/wechat/actions.py` 的 `_providers`），配置中心渲染的是旧快照。
- 企微侧「Agent v2 模型能力」卡片以 `modelCapabilities.result.model` 优先于 `draft.llm.model` 渲染（`src/pages/SettingsPage.tsx`），配置指纹变化后仍把上次检测模型当作当前值。
- 样式现状：`src/index.css`（约 139 KB）承载 token 与大量页面样式，`redesign.css`、`nav.css`、`overlay.css`、`wechat.css`、`groupreply.css` 与页面级 CSS 并行覆盖；token 已有一层（`index.css` 顶部），但页面普遍硬编码颜色/间距，视觉语言不统一。
- 验证设施：前端 vitest 有大量布局/渲染断言（页面 class 名与关键文本被测试锁定），`scripts/ui-preview.mjs` 可用无头 Edge 对真实 React 应用截图（浅色/深色）。
- 发布约束：不在本机打包；`package.json` 是唯一版本源；push `main` 触发 `build-windows.yml` 出 Artifact，push tag `vX.Y.Z` 自动建 Release。

## Goals / Non-Goals

**Goals:**
- 全局 `config.llm` 成为唯一事实来源：个人微信 provider 快照同步、配置中心与能力卡片显示真相。
- 以单层 token 与共享组件统一全应用视觉，保持信息密度与既有功能；浅色/深色、键盘、缩放、最小窗口均可用。
- 文档与 v4.5.0 发布链路同步到位。

**Non-Goals:**
- 不改变页面信息架构与业务流程，不删除既有功能入口；不引入新的 UI 组件库、图标库、网络字体或运行时依赖。
- 不重写 Electron/Python 架构，不改协议字段（`_providers` 新增的 `llm` 已在线上返回，仅前端消费）。
- 不在本机执行 PyInstaller/NSIS/签名，不安装测试安装包。

## Decisions

### D1 模型真相：worker 同步 + 前端消费实时 llm

- worker 侧把 `_ensure_default_provider()` 改为"存在即对齐"：若 `llm-default` 存在且全局 `llm` 的 `baseUrl/model/kind` 有变化，则原地更新该行（保留 `secret_ref` 与用户自定义 provider）；若无 `llm-default` 但已有自定义 provider，则不新建，交由项目选择。
- 前端 `normalizeProviders()` 保留响应中的 `llm`（协议/模型/BaseURL/是否配置/是否有密钥），配置中心「模型连接」卡片优先渲染 `llm` + `来源：全局设置` 标识，快照仅兜底。
- 能力卡片把 `current` 与主值解耦：主值恒为 `draft.llm.model`；上次检测模型仅在"上次检测（已过期）"副行展示，`current` 为真时才显示"检测结果与当前配置一致"。
- 备选：在 UI 层直接读 `config.llm` 而不改 worker——但个人微信常驻 worker 的 provider 行还会被项目/草稿使用，快照会继续误导运行时选择与展示，故一并同步。

### D2 视觉系统：保留现有 CSS 架构，收敛 token 与组件

- 继续使用现有 CSS 文件体系（不迁移 Tailwind-only），但把 `index.css` 顶部 token 扩展为唯一来源：表面/文字/边框/状态色、字号与行高、间距刻度、圆角、两级阴影、动效时长与缓动。
- 页面级硬编码颜色替换为 token 引用；`redesign.css` 只保留壳层与页面级布局，`wechat.css`/`groupreply.css` 等只保留该域特有样式。
- 图标保持 lucide-react（与现有代码一致），字体保持 `Inter + 系统中文字体回退`，不引入外部字体请求（离线桌面应用）。
- 备选：全量迁移 Tailwind 或引入 shadcn——改动面过大且会破坏现有测试与离线可靠性，不采用。

### D3 共享组件与页头模式

- 扩展 `src/components/ui.tsx`：统一 Button 变体（primary/secondary/ghost/danger）、Field 的 label/hint/error、Card/SectionHeader、状态 chip、Empty、Stat；新增样式集中在 `index.css` 的 components 层。
- 统一页头组件模式（eyebrow/h1/description/actions + 可选计数），页面只传内容；sticky 保存条与页面滚动容器解耦，保证单滚动。
- 抽屉/弹窗继续使用 `Overlay`/`overlay.css`，统一尺寸、header/footer、焦点陷阱与遮罩层级。

### D4 页面精修范围与顺序

1. 壳层：titlebar、sidebar（分组/选中态/图标）、page frame、页头与滚动。
2. 个人微信：会话三栏（项目列/会话/草稿）、配置中心（分区块 + 固定保存条 + 高级折叠）。
3. 企微：工作台总览、问题雷达、定时任务、群监听（列表/抽屉/工作队列）。
4. 沟通助手三页、策略与发布四页、设置、关于。
每个页面只调整 CSS 与必要的展示结构；避免改动被测试断言的 class 名与关键文本。

### D5 动效与可访问性

- 统一 160–220ms、`ease-out` 进入、`ease-in` 退出；仅使用 transform/opacity；`prefers-reduced-motion: reduce` 下关闭位移与装饰动效。
- 焦点环使用主色 token，`focus-visible` 全站一致；弹层打开/关闭恢复焦点；遮罩下内容不吞焦点。
- 关键点击目标 ≥ 36px（桌面），图标按钮补齐 `aria-label`/`title`；对比度按 4.5:1 校验，深色单独验证。

### D6 验证策略

- 每个 UI 批次后运行 `pnpm test` 与 `pnpm typecheck`；最终运行 `pnpm build:renderer`。
- 用 `scripts/ui-preview.mjs` 生成浅色/深色关键页面截图（工作台、个人微信会话、配置中心、设置、群监听），人工对照布局溢出/对比度/状态。
- Python 侧运行 `python -m unittest` 相关测试（`tests/` 下 provider/settings 用例）。
- 不在本机执行 PyInstaller/NSIS；打包由 GitHub Actions 完成。

### D7 文档与发布

- README 结构见 proposal；新增 `docs/README.md` 文档索引；同步 `docs/release.md` 的版本/流程描述；CHANGELOG 增加 v4.5.0。
- 版本单源 `package.json` → 4.5.0；main 推送后确认 CI/Windows 构建通过，再打 tag `v4.5.0` 推送触发 Release，用 `gh run watch`/`gh release view` 验证资产。
- 推送凭据：本机 git 未配置 credential helper，使用已登录的 `gh`（`sy118`，repo scope）通过 `gh auth setup-git` 或单次 credential helper 完成推送；不在仓库写入任何凭据。

## Risks / Trade-offs

- [CSS 覆盖冲突导致个别页面回归] → 每批改完跑 vitest/typecheck + 截图对照；优先改 CSS 而非 DOM；保留被测试断言的 class。
- [深色模式对比度不足] → token 成对定义并单独跑深色截图；正文/次要文字按 4.5:1 校验。
- [模型同步影响用户自定义 provider] → 只更新 `llm-default` 行，保留密钥与自定义行；补单测覆盖。
- [UI 改动面大导致一次提交过大] → 拆成"模型修复 / UI 基础层 / 页面批次 / 文档"多次提交，便于回滚。
- [CI 打包耗时或失败] → 先确保本地 test/typecheck/build:renderer 通过；main 构建成功后再打 tag；失败时 `gh run view --log-failed` 定位。

## Migration Plan

- 配置无 schema 变化；provider 同步在 worker 启动时惰性执行，无需数据迁移。
- 发布顺序：本地验证 → 提交 → push main（Artifact 构建）→ 确认成功 → tag `v4.5.0` push（Release）。
- 回滚：UI/文档为独立提交可 revert；发布若需撤回，保留 Release 草稿阶段不发布或删除 tag/Release；不涉及数据库迁移。

## Open Questions

（无）

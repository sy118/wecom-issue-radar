# Proposal

## Why

用户已把全局大模型切换为 `deepseek-v4.1-flash`，但个人微信配置中心仍显示旧的 `qwen3.7-max`，企业微信「设置 → 模型与 OCR → Agent v2 模型能力」卡片也仍显示旧模型名。两处都会让用户误判"当前生效模型"，而群监听、拟稿、问题分析全部按单一全局模型运行——显示与真实不一致就是事故隐患。

同时，v4.x 快速迭代留下了三套并行样式（`index.css` 约 139 KB、`redesign.css`、`wechat.css` 及页面级 CSS），token 被大量覆盖，配置中心与三栏会话在 1280×820 下层级拥挤、主次不清；README 与 docs 也停留在 v4.4.1 的功能堆叠写法。这次一起收口：模型单一事实来源、全应用企业级极简精修、文档重写，并由 GitHub Actions 在线打包发布 v4.5.0。

## What Changes

- **模型单一事实来源**
  - 个人微信 worker 每次 bootstrap 用全局 `config.llm` 刷新 `llm-default` provider 行的 `baseUrl/kind/model/temperature/maxOutputTokens`（保留已保存的 API Key 与自定义 provider），不再"只在表为空时创建一次"。
  - 个人微信配置中心「模型与只读 MCP」改为以实时 `llm` 快照为准渲染，并标注"来源：全局设置"；旧 provider 快照仅作兜底。
  - 「Agent v2 模型能力」卡片在配置指纹不匹配时只显示当前全局模型，把上次检测的模型降级标注为"上次检测（已过期）"，不再让旧模型名充当当前值。
  - 补 Python 单测与前端 vitest 回归：改全局模型后，两处配置中心都显示新模型。
- **桌面端视觉系统（企业级极简）**
  - 建立单层 design token：颜色（浅/深色成对）、字号与行高、4px 间距、圆角、2 级阴影、160–220ms 动效与 reduced-motion。
  - 统一壳层：标题栏、侧边导航（分组、选中态、图标）、页头模式、内容宽度与节奏、sticky 保存条。
  - 收敛共享组件：Button/Input/Field/Card/状态 chip/Drawer·Modal/Empty·Stat，去掉页面级重复覆盖。
  - 精修全部页面：工作台、问题雷达、定时任务、群监听、个人微信（会话草稿+配置中心）、沟通助手三页、策略与发布四页、设置、关于；浅色/深色与键盘焦点、对比度、44px 目标同步验证。
- **文档重写**
  - README 重写为：定位 → 功能地图 → 5 分钟上手 → 数据与安全边界 → 架构 → 开发与验证 → 发布 → FAQ/排障 → 文档索引。
  - 新增 `docs/README.md` 索引，同步 `docs/release.md`、`docs/electron-migration.md`、`docs/agent-runtime-v2.md` 中过时内容。
  - CHANGELOG 增加 v4.5.0 逐项记录。
- **发布**：`package.json` 版本升到 `4.5.0`，推送 `main` 由 CI 构建安装包 Artifact；推送 tag `v4.5.0` 触发 GitHub Release。**不在本机打包**。

## Capabilities

### New Capabilities

- `model-configuration`: 全局模型配置在个人微信配置中心、企业微信模型能力卡片、Agent v2/群监听运行时的显示与生效保持一致；配置中心只读展示当前模型并提供明确的来源标注与过期检测标记。
- `desktop-ui-system`: 桌面应用的视觉与组件系统——语义 token、浅深色、字号与间距节奏、共享组件状态、页面壳层与可访问性基线。

### Modified Capabilities

（无既有 spec；本次为项目首次建立能力清单。）

## Impact

- **前端**：`src/index.css`、`src/redesign.css`、`src/nav.css`、`src/overlay.css`、`src/wechat.css`、`src/groupreply.css`、`src/pages/**/*.css`、`src/components/ui.tsx`、`src/pages/wechat/*`、`src/pages/SettingsPage.tsx`、`src/pages/groupreply/*` 等。
- **后端**：`worker/wechat/service.py`（默认 provider 同步）、`worker/wechat/actions.py`（providers 返回结构保持不变，前端消费 `llm`）。
- **测试**：Python `tests/` 与前端 `*.test.ts(x)` 增加模型一致性回归；`scripts/ui-preview.mjs` 用于浅/深色截图验收。
- **文档**：`README.md`、`docs/*.md`、`CHANGELOG.md`。
- **发布链路**：`package.json`、Git tag `v4.5.0`、GitHub Actions（`build-windows.yml`），无新增运行时依赖、无破坏性配置迁移。

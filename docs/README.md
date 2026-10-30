# 文档索引

本目录收录项目的过程文档与接入契约。第一次使用请先看根目录 [README](../README.md) 的「5 分钟上手」；版本变更记录见 [CHANGELOG](../CHANGELOG.md)。

当前版本 **v4.5.1**。功能描述以代码与 CI 配置为准，发布流程以 `.github/workflows/*.yml` 为准。

## 阅读路径

- **第一次使用 / 排障**：[README](../README.md) → 5 分钟上手 → 数据与安全边界 → 常见问题。
- **理解群监听**：[agent-runtime-v2.md](agent-runtime-v2.md)。
- **参与开发与验证**：[README](../README.md) 的「本地开发与验证」→ [electron-migration.md](electron-migration.md) 的架构与命令。
- **发版与排查 CI**：[release.md](release.md)。
- **从旧版本迁移 / 回滚**：[electron-migration.md](electron-migration.md)。
- **对接外部系统**：[dify-chatflow-api.md](dify-chatflow-api.md)、[tencent-smartsheet-image-fields.md](tencent-smartsheet-image-fields.md)。
- **模型语义阈值校准**：[jev-annotation-set.md](jev-annotation-set.md)。

## 文档清单

| 文档 | 内容 | 主要读者 |
| --- | --- | --- |
| [agent-runtime-v2.md](agent-runtime-v2.md) | 群监听 Agent V2 的执行模型、只读工具安全、上下文与证据、检查点恢复、时间线与诊断边界。 | 维护群监听的开发者、排障人员 |
| [electron-migration.md](electron-migration.md) | Tauri 2 → Electron 45 的架构、开发/构建命令、数据目录、回滚与旧版迁移、已知限制。 | 开发者、需要回滚或迁移的用户 |
| [release.md](release.md) | 版本号规则、GitHub Actions 触发与产物、CI 步骤、Secrets、发布校验、失败处理与回滚。 | 发版负责人 |
| [dify-chatflow-api.md](dify-chatflow-api.md) | Dify Chatflow 的文件上传与对话接口契约、附件处理、错误码与未承诺事项（依据官方文档核验）。 | 接入 Dify 的开发者 |
| [tencent-smartsheet-image-fields.md](tencent-smartsheet-image-fields.md) | 企业微信智能表格「接收外部数据」Webhook 的图片字段契约与 `image_base64` 写法。 | 维护腾讯文档发布的开发者 |
| [jev-annotation-set.md](jev-annotation-set.md) | Jev 语义判断从 `shadow` 切到 `assist` 前的脱敏标注集、基线指标与上线门禁。 | 模型/语义策略维护者 |

## 维护约定

- **版本号唯一来源**：根目录 `package.json` 的 `version` 是唯一来源；README、本索引与 `release.md` 中的版本号随发版同步更新（当前 `4.5.1`）。
- **事实来源**：功能与命令以 `src/`、`electron/`、`worker/`、`scripts/` 和 CI 工作流为准；本文档不描述未实现或已删除的能力。
- **链接**：文档之间使用相对路径；新增、移动或删除文档时，同步更新本索引和根 README 的「文档索引」。
- **安全边界变更**：数据目录、只读/发送边界、密钥与日志策略变化时，同时更新根 README 的「数据与安全边界」与 [electron-migration.md](electron-migration.md)。
- **参考类文档**：`dify-chatflow-api.md` 与 `tencent-smartsheet-image-fields.md` 依赖外部官方契约，更新时保留核验日期与官方来源链接。

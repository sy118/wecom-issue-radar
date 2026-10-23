# personal-wechat-runtime

## ADDED Requirements

### Requirement: 常驻个人微信 worker

系统 SHALL 提供一个常驻的 Python sidecar 模式处理个人微信动作；同一进程内复用运行时、数据库快照缓存、联系人缓存与轮询状态，而不是每个动作启动一次进程。

#### Scenario: 首次动作启动常驻 worker

- **WHEN** 用户第一次触发任何个人微信动作
- **THEN** 主进程懒启动一个常驻 worker，后续动作通过 NDJSON 请求/响应复用该进程

#### Scenario: 崩溃后重启

- **WHEN** 常驻 worker 意外退出且仍有请求或页面在使用个人微信
- **THEN** supervisor 按 100ms 到 5s 的指数退避重启，并将进行中的请求以可读错误返回，不静默丢失

#### Scenario: 退出与更新

- **WHEN** 应用退出或安装更新
- **THEN** supervisor 等待在途请求完成或超时后终止 worker 进程树，不留下孤儿进程

### Requirement: 请求协议与并发

系统 SHALL 使用带请求 id 的 NDJSON 协议关联请求与响应；读请求可以并发，写请求按到达顺序串行，读写的本地数据库访问遵守现有串行保护。

#### Scenario: 并发消息与图片

- **WHEN** 首屏同时请求消息列表和可见图片
- **THEN** 两个请求通过 id 关联返回，互不覆盖；数据库访问不会并发写坏状态

#### Scenario: 写操作顺序

- **WHEN** 用户快速连续保存项目、删除项目或保存草稿
- **THEN** 写请求按顺序执行，每个响应明确成功或失败；系统不会自动重试写请求

### Requirement: 批量首屏接口

系统 SHALL 提供一个 bootstrap 动作，一次返回个人微信首屏需要的检测、项目、设置、模型/MCP 信息和可选的最近消息。

#### Scenario: 一次请求完成首屏

- **WHEN** 用户进入个人微信会话页
- **THEN** 渲染层通过一次 bootstrap 请求获得首屏数据，而不是分别请求 detect、projects、settings、providers、messages

#### Scenario: 不内联图片

- **WHEN** bootstrap 返回最近消息
- **THEN** 响应只包含消息元数据，不包含图片 base64；图片由后续 `media_preview` 按需读取

### Requirement: 降级路径

系统 SHALL 在常驻 worker 不可用时自动回退到一次性 `--request` 通道，保证个人微信功能仍然可用。

#### Scenario: 常驻启动失败

- **WHEN** 常驻 worker 无法启动或连续崩溃超过阈值
- **THEN** 后续个人微信请求回退一次性 worker，并在诊断信息中记录原因

#### Scenario: 一次性通道兼容

- **WHEN** 请求通过一次性 worker 执行
- **THEN** 返回结构与常驻通道一致，渲染层无需区分两种模式

### Requirement: 运行时可观测性

系统 SHALL 暴露或记录常驻 worker 的运行状态，包括启动耗时、重启次数、当前在途请求数和最近错误码。

#### Scenario: 诊断页面查看 worker 状态

- **WHEN** 用户打开设置/诊断相关区域
- **THEN** 可以看到当前个人微信 worker 模式（常驻/一次性）、启动耗时和最近一次错误摘要

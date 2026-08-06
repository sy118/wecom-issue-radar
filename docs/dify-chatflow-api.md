# Dify Chatflow API 接入契约：文件上传与对话

> 核验日期：2026-08-06。本文只采用 Dify 官方文档及其公开 OpenAPI 契约；中文 Chatflow 指南标注为自动翻译，因此字段名、状态码和事件名以同页 OpenAPI 契约为准。

## 结论

- Dify Cloud 的 API 基础 URL 是 `https://api.dify.ai/v1`；自部署使用自身实例的 API 基础 URL。本文涉及的两个调用分别是 `POST /files/upload` 和 `POST /chat-messages`，均使用应用 API Key，通过 `Authorization: Bearer {API_KEY}` 认证。[官方文档：快速开始](https://docs.dify.ai/zh/api-reference/guides/get-started) [官方文档：上传文件](https://docs.dify.ai/zh/api-reference/files/upload-file) [官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)
- 本地图片或其他文件需要先上传，再把响应中的 `id` 作为 `files[].upload_file_id` 发给对话接口；远程图片也可以直接用 `transfer_method: "remote_url"` 和 `url` 引用。[官方文档：上传文件](https://docs.dify.ai/zh/api-reference/files/upload-file) [官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)
- 文件上传和对话请求必须使用同一个稳定的 `user`。Dify 用它隔离终端用户的会话、消息和文件；不同 `user` 不能引用对方上传的文件。[官方文档：终端用户身份](https://docs.dify.ai/zh/api-reference/guides/end-user-identity)
- 文本对话通过 `query` 发送，工作流声明的输入变量通过 `inputs` 发送，图片等附件通过 `files` 发送。公开的 `/chat-messages` 请求契约没有 `messages` 或“外部历史消息数组”字段；Dify 的多轮上下文由 `conversation_id` 维持。[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message) [官方文档：Chatflow 应用 API](https://docs.dify.ai/zh/api-reference/guides/chatflow)
- Chatflow 支持 `blocking` 和 `streaming`。官方推荐流式模式；阻塞模式可能因长耗时而被代理中断，官方没有给出 Dify Cloud 边缘代理超时的具体秒数。[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message) [官方文档：流式响应](https://docs.dify.ai/zh/api-reference/guides/streaming)

## 认证与地址

| 项目 | 官方契约 |
| --- | --- |
| Dify Cloud 基础 URL | `https://api.dify.ai/v1` |
| 自部署基础 URL | 当前 Dify 实例自己的 API 基础 URL |
| 上传文件 | `POST {base_url}/files/upload` |
| 发送对话 | `POST {base_url}/chat-messages` |
| 认证头 | `Authorization: Bearer {API_KEY}` |
| Key 类型 | 应用接口使用该应用自己的应用 API Key |

每个已发布应用都可作为 REST API 使用；应用 API Key 只作用于该应用，一个 Key 可服务多个终端用户。Key 应只保存在服务端，不应放入前端或客户端。缺失或无效 Key 返回 HTTP `401`、错误码 `unauthorized`。[官方文档：快速开始](https://docs.dify.ai/zh/api-reference/guides/get-started) [官方文档：上传文件的认证契约](https://docs.dify.ai/zh/api-reference/files/upload-file)

## 调用流程

### 1. 上传每个本地文件

请求必须是 `multipart/form-data`：

```bash
curl --request POST \
  --url 'https://api.dify.ai/v1/files/upload' \
  --header 'Authorization: Bearer {API_KEY}' \
  --form 'file=@capture.png' \
  --form 'user=wecom-user-123'
```

| multipart 字段 | 必填 | 含义 |
| --- | --- | --- |
| `file` | 是 | 单个二进制文件；文件名不能包含 `/` 或 `\`，文件 part 必须声明 MIME 类型 |
| `user` | 是 | 应用内唯一的终端用户标识；必须与随后 `/chat-messages` 使用的 `user` 完全一致 |

一次请求只允许一个文件。部署安全黑名单之外的文件类型可以上传，但应用实际能使用的文件类别仍取决于应用的文件上传设置。默认大小限制为：图片 10 MB、音频 50 MB、视频 100 MB、其他文件 15 MB；自部署可通过 `UPLOAD_*_FILE_SIZE_LIMIT` 环境变量调整。[官方文档：上传文件](https://docs.dify.ai/zh/api-reference/files/upload-file)

成功时返回 HTTP `201`，响应对象的 `id` 是后续对话引用的唯一文件 ID：

```json
{
  "id": "a1b2c3d4-5678-90ab-cdef-1234567890ab",
  "name": "capture.png",
  "size": 204800,
  "extension": "png",
  "mime_type": "image/png",
  "created_at": 1705407629
}
```

完整响应还公开 `reference`、`created_by`、`preview_url`、`source_url`、`original_url`、`tenant_id`、`conversation_id` 等字段；用于对话附件引用的是 `id`，不是 `source_url` 或 `reference`。[官方文档：上传文件](https://docs.dify.ai/zh/api-reference/files/upload-file)

### 2. 发送文本、输入变量和附件

本地上传图片的最小 Chatflow 请求如下：

```json
{
  "inputs": {},
  "query": "请结合这张图片和本轮企业微信对话进行分析。",
  "response_mode": "streaming",
  "conversation_id": "",
  "user": "wecom-user-123",
  "files": [
    {
      "type": "image",
      "transfer_method": "local_file",
      "upload_file_id": "a1b2c3d4-5678-90ab-cdef-1234567890ab"
    }
  ]
}
```

远程图片的引用形式是：

```json
{
  "type": "image",
  "transfer_method": "remote_url",
  "url": "https://example.com/capture.png"
}
```

`POST /chat-messages` 使用 `application/json`。官方 OpenAPI 请求字段如下：[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)

| 字段 | 必填 | 官方语义 |
| --- | --- | --- |
| `query` | 是 | 当前终端用户的文本消息 |
| `inputs` | 是 | 应用输入变量对象，键名和类型来自“获取应用参数”的 `user_input_form`；没有变量时传 `{}` |
| `user` | 是 | 应用定义且在应用内唯一的终端用户标识 |
| `response_mode` | 否 | `streaming` 或 `blocking`；省略时默认 `blocking` |
| `conversation_id` | 否 | 要继续的会话 ID；省略或传空字符串会新建会话 |
| `files` | 否 | 本轮附件数组 |
| `auto_generate_name` | 否 | 是否自动生成会话标题，默认 `true` |
| `workflow_id` | 否 | 仅 Chatflow；指定要执行的已发布工作流版本，省略时用最新已发布版本 |

`files[]` 必须包含 `type` 和 `transfer_method`。`type` 的公开枚举是 `image`、`document`、`audio`、`video`、`custom`；`transfer_method` 是 `local_file` 或 `remote_url`。前者要求 `upload_file_id`，后者要求 `url`。[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)

## 三类 ID 不可混用

| ID | 生命周期与用途 | 获取位置 | 后续用途 |
| --- | --- | --- | --- |
| `conversation_id` | 跨多轮消息的会话上下文 | `/chat-messages` 的阻塞响应或流式事件 | 下一轮 `/chat-messages` 原样传回，以继续此前对话 |
| `workflow_run_id` | 一次持久化的 Chatflow/Workflow 运行记录 | 流中的工作流和节点事件 | 流断开或人工介入暂停后，调用“流式获取工作流事件”恢复跟踪；也用于查询运行详情 |
| `task_id` | 当前正在进行的生成任务 | 阻塞响应或除 `error` 外的流式事件 | 调用“停止响应”接口；不是会话 ID，也不是持久运行 ID |

这些标识符的用途由官方分别定义；特别是断线恢复需要保存 `workflow_run_id`，不能拿 `task_id` 代替，而下一轮多轮对话仍使用 `conversation_id`。[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message) [官方文档：流式响应](https://docs.dify.ai/zh/api-reference/guides/streaming)

## 响应结构

### 阻塞模式

`response_mode: "blocking"` 成功时返回 HTTP `200`、`Content-Type: application/json`，并在工作流运行结束后一次性返回完整 `answer`：

```json
{
  "event": "message",
  "task_id": "c3800678-a077-43df-a102-53f23ed20b88",
  "id": "b01a39de-3480-4f3e-9f1e-4841a80f8e5e",
  "message_id": "9da23599-e713-473b-982c-4328d4f5c78a",
  "conversation_id": "45701982-8118-4bc5-8e9b-64562b4555f2",
  "mode": "advanced-chat",
  "answer": "完整回复文本",
  "metadata": {
    "usage": {},
    "retriever_resources": []
  },
  "created_at": 1705407629
}
```

公开 schema 还明确：`task_id` 用于请求跟踪和停止响应，`message_id` 用于反馈或建议问题接口；Chatflow 的 `mode` 为 `advanced-chat`。[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)

### 流式模式

`response_mode: "streaming"` 成功建立连接时返回 HTTP `200`、`Content-Type: text/event-stream`。每个业务事件是一个 `data: ` 行中的 JSON 对象，并以空行分隔；每 10 秒还会出现不带 JSON `data` 的裸 `event: ping` 保活行。[官方文档：流式响应](https://docs.dify.ai/zh/api-reference/guides/streaming)

Chatflow 成功运行的主要事件顺序是：

```text
data: {"event":"workflow_started", ...}
data: {"event":"node_started", ...}
data: {"event":"message", "answer":"分片文本", ...}
data: {"event":"node_finished", ...}
data: {"event":"message_end", "metadata":{...}, ...}
data: {"event":"workflow_finished", "data":{"status":"succeeded", ...}, ...}
```

客户端应按到达顺序拼接 `message.answer`，并在 `message_replace` 到达时替换此前累计文本。不同 Dify 版本与工作流可能交换 `message_end` 和 `workflow_finished` 的先后顺序，因此本项目不依赖二者顺序，只在两者均已到达且工作流状态为 `succeeded` 后判定成功。工作流或节点事件的载荷位于 `data` 对象中。除 `ping` 外，事件通常包含会话、消息或任务标识；工作流和节点事件还会提供 `workflow_run_id`。[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)

Chatflow 失败时，官方事件契约是 `workflow_finished`（`data.status: "failed"`）之后发送 `error`，且不发送 `message_end`。运行到人工介入节点时会发送 `human_input_required`，随后以 `workflow_paused` 结束当前流。[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message) [官方文档：Chatflow 应用 API](https://docs.dify.ai/zh/api-reference/guides/chatflow)

## 会话连续性与用户隔离

1. 第一次调用省略 `conversation_id` 或传 `""`，Dify 创建新会话，并在阻塞响应或流式事件中返回 `conversation_id`。[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)
2. 后续调用把该 `conversation_id` 原样传回，并继续使用同一个 `user`。Chatflow 会保留此前轮次作为上下文，使新消息可以引用更早内容。[官方文档：Chatflow 应用 API](https://docs.dify.ai/zh/api-reference/guides/chatflow) [官方文档：终端用户身份](https://docs.dify.ai/zh/api-reference/guides/end-user-identity)
3. `user` 是调用方提供的稳定标识，Dify 不会认证它。会话、消息、文件都只对相同 `user` 的请求可见；上传、发送、停止和恢复调用应保持同一 `user`。[官方文档：终端用户身份](https://docs.dify.ai/zh/api-reference/guides/end-user-identity)
4. API 创建的用户与 Dify 托管 Web App 的用户身份彼此分离；API 会话不会出现在 Web App 中，反之亦然。[官方文档：终端用户身份](https://docs.dify.ai/zh/api-reference/guides/end-user-identity)

## 错误与超时语义

### 文件上传

| HTTP | 错误码 | 官方含义 |
| --- | --- | --- |
| `400` | `no_file_uploaded` | 没有提供文件 |
| `400` | `too_many_files` | 一次请求上传了多个文件 |
| `400` | `filename_not_exists_error` | 文件没有文件名 |
| `400` | `invalid_param` | 文件名含 `/` 或 `\`，或扩展名在部署黑名单中 |
| `413` | `file_too_large` | 超过文件类别大小限制；当前运行时 `message` 可能为空，应以状态码和 `code` 为准 |
| `415` | `unsupported_file_type` | `file` part 未声明 MIME 类型 |
| `401` | `unauthorized` | API Key 缺失或无效 |

[官方文档：上传文件](https://docs.dify.ai/zh/api-reference/files/upload-file)

### 发送对话

| HTTP | 错误码 | 官方含义 |
| --- | --- | --- |
| `400` | `app_unavailable`、`not_chat_app` | 应用不可用/配置错误，或应用模式与接口不匹配 |
| `400` | `provider_not_initialize`、`provider_quota_exceeded`、`model_currently_not_support`、`completion_request_error` | 模型提供商凭据、配额、模型可用性或文本生成失败 |
| `400` | `bad_request`、`invalid_param`、`agent_not_published` | 工作流版本/ID 或新 Agent 配置、响应模式不合法 |
| `400` | `conversation_completed` | 会话已结束；省略 `conversation_id` 开始新会话 |
| `403` | `workflow_version_execution_not_allowed` | Dify Cloud Sandbox 方案不允许用 `workflow_id` 固定工作流版本 |
| `404` | `not_found` | 会话不存在，或指定的工作流不存在 |
| `429` | `too_many_requests`、`rate_limit_error` | 应用并发请求过多，或 Dify Cloud 工作流执行配额耗尽 |
| `500` | `internal_server_error` | 服务端内部错误 |
| `401` | `unauthorized` | API Key 缺失或无效 |

[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)

流建立以后发生的错误不会把 HTTP 状态从 `200` 改成其他值：节点失败通过 `node_finished`/`workflow_finished` 的 `status: "failed"` 表达，其他失败以终止流的 `error` 事件表达，其关键字段是 `status`、`code`、`message`。客户端必须同时处理这两类失败信号。[官方文档：流式响应](https://docs.dify.ai/zh/api-reference/guides/streaming)

阻塞模式只适合较短调用；官方说明 Dify Cloud 边缘代理可能在上游响应未在其超时内到达时断开连接，但没有公开具体超时秒数。流式模式每 10 秒发送 `ping`，客户端读超时应明显高于 10 秒。[官方文档：流式响应](https://docs.dify.ai/zh/api-reference/guides/streaming)

对于 Chatflow 这类工作流支持的运行，流连接意外断开后，可保存并使用事件中的 `workflow_run_id`、同一个 `user` 调用“流式获取工作流事件”重新连接；`user` 不匹配时返回 `404`。官方同时建议在重连到仍运行的工作流后，用“获取工作流执行情况”确认最终完成状态。[官方文档：流式响应](https://docs.dify.ai/zh/api-reference/guides/streaming)

## 官方文档没有明确承诺的事项

- `/chat-messages` 的公开请求 schema 没有结构化 `messages[]` 历史数组，也没有企业微信消息 ID、发送者、群聊等专用字段。把一批外部对话格式化进 `query`，或映射到 Chatflow 已声明的 `inputs`，属于本项目的接入设计，不是 Dify API 规定的格式。[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)
- 官方没有规定企业微信用户/群与 Dify `user`、`conversation_id` 的映射方式。只规定 `user` 应稳定且在应用内唯一，以及继续会话时复用返回的 `conversation_id`。[官方文档：终端用户身份](https://docs.dify.ai/zh/api-reference/guides/end-user-identity) [官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)
- 核验页面没有给出 Dify Cloud 阻塞请求的具体超时数值，也没有给出这两个 POST 接口的幂等键、自动重试或“恰好一次”保证。不能据此假定超时后请求一定没有在服务端继续执行。[官方文档：流式响应](https://docs.dify.ai/zh/api-reference/guides/streaming) [官方文档：上传文件](https://docs.dify.ai/zh/api-reference/files/upload-file) [官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)
- `/chat-messages` schema 本身没有声明一个适用于所有应用的全局固定附件上限。图片的实际应用配置应通过 `GET /parameters` 读取，其中 `file_upload.image` 会返回 `enabled`、`number_limits` 和 `transfer_methods`；因此接入层不应把示例数量当作固定上限。核验页面也没有给出附件处理顺序保证或重复附件去重语义。[官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message) [官方文档：获取应用参数](https://docs.dify.ai/zh/api-reference/applications/get-app-parameters)
- 兼容性实测发现，部分自部署版本的 `GET /parameters` 会报告图片/文件上传 `enabled=false`，但同一应用的 `POST /files/upload` 仍成功返回 HTTP `201`。因此本项目把参数接口的开关与传输方式作为工作台提示和数量参考，不据此在网络请求前硬拒绝附件；真实格式与大小能力以上传接口的响应为准。
- 上传接口允许某类文件，不等于当前 Chatflow 一定会理解其内容。官方只承诺上传并引用，实际可用类型取决于应用文件上传设置；具体模型、节点和工作流如何处理附件不在这两个接口的保证范围内。[官方文档：上传文件](https://docs.dify.ai/zh/api-reference/files/upload-file) [官方文档：Chatflow 应用 API](https://docs.dify.ai/zh/api-reference/guides/chatflow)
- Dify API 只定义上述传输、会话和事件契约，并不承诺接入后与现有 “MCP + 提示词” 路径产生相同结果。要保持业务流程与输出语义一致，需要在 Dify 中配置等价的 Chatflow，并由本项目在两种执行器之间做显式路由。[官方文档：Chatflow 应用 API](https://docs.dify.ai/zh/api-reference/guides/chatflow) [官方文档：发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)

## 官方来源

- Dify，[上传文件](https://docs.dify.ai/zh/api-reference/files/upload-file)
- Dify，[Chatflow 应用 API](https://docs.dify.ai/zh/api-reference/guides/chatflow)
- Dify，[发送对话消息](https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message)
- Dify，[快速开始](https://docs.dify.ai/zh/api-reference/guides/get-started)
- Dify，[终端用户身份](https://docs.dify.ai/zh/api-reference/guides/end-user-identity)
- Dify，[流式响应](https://docs.dify.ai/zh/api-reference/guides/streaming)
- Dify，[获取应用参数](https://docs.dify.ai/zh/api-reference/applications/get-app-parameters)

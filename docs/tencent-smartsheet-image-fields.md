# 企业微信智能表格 Webhook 图片字段

> 核验日期：2026-07-24。本文只使用企业微信开发者中心的官方文档作为依据。

## 结论

- Smart Sheet “接收外部数据” Webhook 的图片字段值类型是 `Object[](CellImageValue)`。官方公开契约中，`CellImageValue` 只有两个字段：`title: string` 和 `image_base64: string`。[^add-records]
- 公开契约没有 `image_url`、`id`、`width`、`height`、`media_id` 或文件 token。因此外部 URL 不是官方文档保证的写入方式，宽高也不是必填项。[^add-records]
- 建议直接将图片原始字节做 Base64 编码，以 `image_base64` 写入；不要先上传到 `media/uploadimg` 再把返回 URL 写入 Smart Sheet。官方将 `image_base64` 定义为“图片内容的 base64 编码”，未记载 `data:image/...;base64,` 前缀形式，因此最保守的传值是不带 data-URL 前缀的纯 Base64 字符串。[^add-records]

## 建议 payload

```json
{
  "add_records": [
    {
      "values": {
        "FIELD_ID": [
          {
            "title": "capture.png",
            "image_base64": "iVBORw0KGgoAAAANSUhEUgAA..."
          }
        ]
      }
    }
  ]
}
```

`FIELD_ID` 必须替换为当前工作表图片字段的真实 ID。`title` 建议保留真实扩展名，`image_base64` 传图片字节的 Base64 文本。顶层 `add_records` 和每条记录的 `values` 结构也来自同一官方“添加记录”文档。[^add-records]

## 为什么 `wework.qpic.cn` URL 可能显示不可访问

`/cgi-bin/media/uploadimg` 的官方文档确实说会返回“永久有效”的图片 URL，但同页立即限定了用途：返回 URL “仅能用于图文消息正文中的图片展示，或者给客户发送欢迎语等”；用于“非企业微信环境下的页面”会被屏蔽。[^uploadimg]

这意味着：

1. HTTP 200 或浏览器中偶尔可打开，不等于该 URL 被 Smart Sheet 图片单元格的官方契约支持。
2. Smart Sheet 服务端抓取、转存或预览图的访问场景可能触发 qpic 的用途/环境限制，从而出现“不可访问”、空白或延迟显示。这是基于官方限制与公开契约的工程判断，官方没有声明 `media/uploadimg` URL 可用于 Smart Sheet Webhook。
3. 即使某次 `image_url` 在等待后成功显示，它也属于未文档化的兼容行为，不应作为稳定接口依赖。

## 是否官方说明了延迟/异步渲染

没有。当前“添加记录”公开文档只说明请求结构、返回的 `errcode`/`errmsg`/`add_records` 和单元格值类型，没有给出外部图片 URL 抓取、图片转存或预览生成的时延保证。[^add-records] 因此“稍等后可见”可以记为现象，但不能当作官方 SLA 或 payload 合规的证据。

## 官方来源

[^add-records]: 企业微信开发者中心，[接收外部数据到智能表格 — 添加记录](https://developer.work.weixin.qq.com/document/path/101240)。关键段落：`Value` 表中“图片 (FIELD_TYPE_IMAGE) -> Object[](CellImageValue)”，以及 `CellImageValue` 表中 `title: string` / `image_base64: string`。
[^uploadimg]: 企业微信开发者中心，[素材管理 — 上传图片](https://developer.work.weixin.qq.com/document/path/90256)。关键段落：返回 URL 永久有效，但仅能用于文档明确列出的企业微信消息/欢迎语场景，非企业微信环境页面会被屏蔽。

# pymss Server 错误格式

pymss server 返回 JSON（JavaScript Object Notation，文本对象表示格式）错误对象。客户端可通过 HTTP（HyperText Transfer Protocol，超文本传输协议）状态码判断错误大类，并通过 `error.code` 执行业务分支处理。

错误对象格式如下：

```json
{
  "error": {
    "message": "Model 'foo' is not loaded by this process.",
    "type": "invalid_request_error",
    "param": "model",
    "code": "model_not_found"
  }
}
```

下表说明错误对象字段。`error.code` 是面向客户端处理逻辑的稳定字段。

| 字段 | 说明 |
|---|---|
| `error.message` | 可读错误信息 |
| `error.type` | 错误类别，取值包括 `invalid_request_error` 和 `server_error` |
| `error.param` | 相关参数名；无明确参数时为 `null` |
| `error.code` | 稳定错误码 |

## 错误码

下表列出 server 代码中显式返回的错误码。`type=invalid_request_error` 表示请求、状态或资源限制导致的错误；`type=server_error` 表示下载、模型加载、推理或响应编码过程中的 server 侧失败。

| HTTP | code | type | 说明 |
|---:|---|---|---|
| 400 | `invalid_request` | `invalid_request_error` | JSON 结构无效、字段类型错误或 `Content-Length` 非法 |
| 400 | `invalid_query_parameter` | `invalid_request_error` | 二进制 PCM 请求的 query 参数格式无效 |
| 400 | `invalid_model` | `invalid_request_error` | `model` 为空，或模型不支持加载和推理 |
| 404 | `model_not_found` | `invalid_request_error` | `model` 未匹配当前已加载模型的 catalog name，或 catalog 中不存在 |
| 400 | `invalid_audio_format` | `invalid_request_error` | `format` 必须为 `pcm_f32le` 或 `pcm_s16le` |
| 400 | `invalid_sample_rate` | `invalid_request_error` | 请求 sample rate 与模型 sample rate 不一致 |
| 400 | `invalid_channel_count` | `invalid_request_error` | `channels` 必须为 `1` 或 `2` |
| 400 | `invalid_base64` | `invalid_request_error` | JSON `input.data` 必须是合法 base64 |
| 400 | `empty_audio` | `invalid_request_error` | decoded PCM bytes 为空 |
| 400 | `invalid_audio_length` | `invalid_request_error` | PCM bytes 长度无法按 format 和 channels 对齐 |
| 400 | `invalid_audio_data` | `invalid_request_error` | `pcm_f32le` 输入包含 NaN（Not a Number，非数字）或 Inf（Infinity，无穷大） |
| 400 | `missing_audio_metadata` | `invalid_request_error` | 二进制 PCM 请求缺少 `format`、`sample_rate` 或 `channels` |
| 415 | `unsupported_content_type` | `invalid_request_error` | `Content-Type` 必须为 `application/json` 或 `application/octet-stream` |
| 400 | `invalid_stem` | `invalid_request_error` | 请求了模型不支持的 stem，或 `stems` 类型非法 |
| 400 | `invalid_response_format` | `invalid_request_error` | `response_format` 必须为 `json` 或 `zip` |
| 400 | `invalid_output_audio_format` | `invalid_request_error` | `output_audio_format` 不受支持，或 JSON 响应请求了非 `pcm_f32le` 输出 |
| 413 | `request_too_large` | `invalid_request_error` | 请求体或音频时长超过 server 限制 |
| 401 | `invalid_api_key` | `invalid_request_error` | Bearer token（Bearer 令牌）缺失或不匹配 |
| 429 | `server_overloaded` | `invalid_request_error` | 推理队列已满 |
| 409 | `model_operation_in_progress` | `invalid_request_error` | 模型加载、卸载或切换正在进行 |
| 409 | `model_download_in_progress` | `invalid_request_error` | 模型下载正在进行 |
| 503 | `model_not_loaded` | `invalid_request_error` | 当前没有已加载模型 |
| 400 | `invalid_inference_parameter` | `invalid_request_error` | 运行时加载参数未知、格式非法，或当前模型配置不支持 |
| 400 | `invalid_download_source` | `invalid_request_error` | 下载源或下载 endpoint 非法 |
| 504 | `separation_timeout` | `invalid_request_error` | 推理超过 `--request-timeout-seconds` |
| 500 | `model_unload_failed` | `server_error` | 卸载当前模型失败 |
| 500 | `model_load_failed` | `server_error` | 加载请求模型失败 |
| 500 | `model_download_failed` | `server_error` | 下载模型失败 |
| 500 | `separation_failed` | `server_error` | 推理或响应编码过程失败 |
| 500 | `webui_assets_missing` | `server_error` | 启用了 WebUI（Web User Interface，浏览器用户界面），但构建后的 WebUI 静态资源缺失 |

## 资源限制相关错误

资源限制由启动参数控制。下表列出限制项和对应错误。

| 限制项 | 参数 | 错误 |
|---|---|---|
| HTTP body 大小 | `--max-request-bytes` | 超过限制时返回 `413 request_too_large` |
| decoded PCM 时长 | `--max-audio-seconds` | 超过限制时返回 `413 request_too_large`；参数为 `0` 时关闭该时长限制 |
| 推理队列长度 | `--max-queue-size` | 正在处理和等待处理的请求数量达到限制时返回 `429 server_overloaded` |
| 单请求推理耗时 | `--request-timeout-seconds` | 超过限制时返回 `504 separation_timeout`；参数为 `0` 时关闭该超时限制 |

推理执行由单模型锁串行化。`--max-queue-size` 统计正在处理和等待处理的请求数量，因此一个请求进入推理锁等待区后仍占用队列额度。

## 模型操作冲突

模型加载或切换期间，server 将公开状态中的已加载模型置空，并将 `/health` 中的 `model_loading` 设为 `true`。该状态下，推理请求返回 `409 model_operation_in_progress`，`/v1/models` 返回空列表。

模型下载、模型加载和模型切换互斥。模型下载期间，另一个下载请求返回 `409 model_download_in_progress`，加载请求返回 `409 model_download_in_progress`。模型加载或切换期间，下载请求返回 `409 model_operation_in_progress`。

## 鉴权错误

API key（Application Programming Interface key，接口访问密钥）只作用于 `/v1/*` 端点。server 设置 `--api-key` 后，客户端必须发送 Bearer token（Bearer 令牌）：

```http
Authorization: Bearer <api-key>
```

令牌缺失或不匹配时，`/v1/*` 端点返回 `401 invalid_api_key`。`/health` 不要求鉴权。

# pymss Server API

pymss server 通过 HTTP（HyperText Transfer Protocol，超文本传输协议）API（Application Programming Interface，应用程序接口）管理模型、查询 catalog（模型目录）、下载模型文件并执行音频源分离。server 以单模型方式运行，一个进程最多持有一个 `MSSeparator`（pymss 的模型推理封装）实例。启动时可以省略模型，此时 server 进入空载状态，并可通过 HTTP API 加载模型。

## HTTP 端点总览

下表列出 server 暴露的 HTTP 端点。`/v1/*` 端点在配置 API key（Application Programming Interface key，接口访问密钥）时使用 Bearer token（Bearer 令牌）鉴权；`/health` 和 WebUI（Web User Interface，浏览器用户界面）路由不使用该鉴权逻辑。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---:|---|
| `GET` | `/health` | 否 | 返回进程健康状态 |
| `GET` | `/v1/models` | 是 | 返回当前进程暴露的模型列表 |
| `GET` | `/v1/models/{model}` | 是 | 返回指定已加载模型的元数据 |
| `GET` | `/v1/catalog/models` | 是 | 返回 catalog 模型及本地文件状态 |
| `GET` | `/v1/catalog/models/{model}` | 是 | 返回指定 catalog 模型详情 |
| `GET` | `/v1/server/info` | 是 | 返回 WebUI 和 server 配置信息 |
| `POST` | `/v1/models/load` | 是 | 加载或切换模型 |
| `POST` | `/v1/models/download` | 是 | 下载单个 catalog 模型到 model dir |
| `GET` | `/v1/download-source` | 是 | 返回当前默认下载源 |
| `POST` | `/v1/download-source` | 是 | 切换当前默认下载源 |
| `POST` | `/v1/audio/separations` | 是 | 执行音频源分离 |
| `GET` | `/ui` | 否 | 启用 `--webui` 时重定向到 `/ui/` |
| `GET` | `/ui/` | 否 | 启用 `--webui` 时返回 WebUI 入口 HTML（HyperText Markup Language，超文本标记语言） |
| `GET` | `/ui/assets/{asset_path}` | 否 | 启用 `--webui` 时返回 WebUI 静态资源 |
| `GET` | `/ui/{path}` | 否 | 启用 `--webui` 时返回单页应用入口 HTML |

表中“鉴权”为“是”的端点只在启动参数 `--api-key` 存在时要求令牌。`--api-key` 为空时，`/v1/*` 端点接受未携带 `Authorization` header 的请求。

设置 `--api-key` 后，客户端在 `Authorization` header 中发送 Bearer token：

```http
Authorization: Bearer <api-key>
```

## GET /health

`/health` 返回 server 进程的健康状态，并直接读取当前公开模型状态。该端点不要求鉴权。

响应示例：

```json
{
  "status": "ok",
  "model_loaded": true,
  "model_loading": false,
  "model": "bs_roformer_voc_hyperacev2.ckpt",
  "device": "cuda:0"
}
```

`model_loaded` 表示当前公开状态中存在已加载模型。空载状态或模型加载、切换期间，该字段为 `false`。`model_loading` 表示模型加载或切换操作正在执行；`/health` 在该状态下仍直接返回。

## GET /v1/models

`/v1/models` 返回当前进程暴露的模型列表。server 以单模型方式运行，因此响应中的 `data` 要么为空列表，要么包含一个模型对象。

响应示例：

```json
{
  "object": "list",
  "data": [
    {
      "id": "bs_roformer_voc_hyperacev2.ckpt",
      "object": "model",
      "created": 0,
      "owned_by": "pymss",
      "pymss": {
        "catalog_name": "bs_roformer_voc_hyperacev2.ckpt",
        "model_type": "bs_roformer",
        "architecture": "bs_roformer",
        "category": "vocal/vocal_extraction",
        "catalog_target_stem": "vocals",
        "supported": true,
        "sample_rate": 44100,
        "instruments": ["vocals", "instrument"],
        "instruments_source": "separator.config.training.instruments",
        "supported_parameters": {
          "inference": ["batch_size", "standardize", "normalize"],
          "audio": ["chunk_size"]
        }
      }
    }
  ]
}
```

响应中的 `id` 固定为已加载模型的 catalog `name`。`pymss.instruments` 来自已加载模型的 `separator.config.training.instruments`，并作为 `/v1/audio/separations` 的 `stems` 可选值；stem 表示分离音轨名称。`pymss.catalog_target_stem` 保留 catalog 中的 `target_stem` 值。

## GET /v1/models/{model}

`/v1/models/{model}` 返回指定已加载模型的元数据。路径参数 `model` 必须匹配当前已加载模型的 catalog `name`。

响应对象与 `/v1/models` 的 `data[]` 项一致。空载状态或路径参数未匹配当前已加载模型时，server 返回 `404 model_not_found`。

## GET /v1/server/info

`/v1/server/info` 返回 WebUI 首屏使用的只读 server 信息，并遵守 `/v1/*` 的 Bearer token 鉴权规则。

响应示例：

```json
{
  "object": "server.info",
  "webui": {
    "enabled": true,
    "path": "/ui/"
  },
  "auth": {
    "api_key_required": true
  },
  "limits": {
    "max_audio_seconds": 600.0,
    "max_request_bytes": 536870912,
    "max_queue_size": 8,
    "request_timeout_seconds": 0.0
  },
  "download_source": {
    "source": "modelscope",
    "endpoint": null
  }
}
```

该端点保持 server 状态不变。`limits` 字段供客户端展示和预检查；请求处理阶段仍按对应限制执行校验。

## GET /v1/catalog/models

`/v1/catalog/models` 列出 pymss 包内 catalog 模型，并返回每个模型在 server 配置的 model dir（模型缓存目录）下的本地文件状态。已加载模型信息由 `/v1/models` 表示。

下表列出查询参数。过滤参数决定返回哪些 catalog entry，`source` 和 `endpoint` 只影响响应中的远程 URL（Uniform Resource Locator，统一资源定位符）。

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `supported` | `true` | `true`、`false` 或 `all` |
| `category` | 空 | 按 primary category、secondary category 或 `primary/secondary` 过滤 |
| `local` | `all` | `all`、`complete` 或 `missing` |
| `q` | 空 | 按 `name`、alias、architecture、target stem 做大小写不敏感搜索 |
| `include_files` | `false` | 是否返回每个模型的 required file 状态 |
| `source` | 当前默认源 | 生成 `remote_url` 时使用的下载源 |
| `endpoint` | 当前默认 endpoint | 生成 `remote_url` 时使用的自定义 endpoint |

这些查询参数只作用于本次列表请求；server 的默认下载源由 `/v1/download-source` 管理。

响应示例：

```json
{
  "object": "list",
  "data": [
    {
      "id": "bs_roformer_voc_hyperacev2.ckpt",
      "object": "pymss.model_catalog_entry",
      "owned_by": "pymss",
      "pymss": {
        "name": "bs_roformer_voc_hyperacev2.ckpt",
        "aliases": ["bs_roformer_voc_hyperacev2.ckpt", "bs_roformer_voc_hyperacev2"],
        "model_type": "bs_roformer",
        "architecture": "bs_roformer",
        "category": "vocal/vocal_extraction",
        "primary_category": "vocal",
        "secondary_category": "vocal_extraction",
        "target_stem": "vocals",
        "supported": true,
        "unsupported_reason": "",
        "size_bytes": 123456789,
        "local": {
          "complete": false,
          "missing_count": 1
        },
        "remote": {
          "available": true,
          "source": "modelscope",
          "endpoint": null
        }
      }
    }
  ],
  "pymss": {
    "source": "modelscope",
    "endpoint": null,
    "total": 1
  }
}
```

响应中的 `id` 固定为 catalog entry 的 `name`。`local.complete` 基于模型主文件、config 文件和 auxiliary 文件的存在性计算。`size_bytes` 对模型主文件使用 catalog 记录；`pymss.files` 中的 config 文件和 auxiliary 文件大小来自本地文件状态，缺失文件大小为 `0`。列表响应一次返回全部匹配项。

`remote` 字段由随包发布的 catalog 和请求中的下载源参数生成。`remote.available=true` 表示该模型存在于 catalog 中。

## GET /v1/catalog/models/{model}

`/v1/catalog/models/{model}` 查询单个 catalog 模型。路径参数 `model` 可以是 catalog `name`、alias 或 stem。响应 `id` 始终是 catalog `name`。

详情响应与列表项同结构，并固定包含 `pymss.files`。下面的示例展示了 `files` 中的模型主文件状态。

```json
{
  "id": "bs_roformer_voc_hyperacev2.ckpt",
  "object": "pymss.model_catalog_entry",
  "owned_by": "pymss",
  "pymss": {
    "name": "bs_roformer_voc_hyperacev2.ckpt",
    "aliases": ["bs_roformer_voc_hyperacev2.ckpt", "bs_roformer_voc_hyperacev2"],
    "model_type": "bs_roformer",
    "architecture": "bs_roformer",
    "category": "vocal/vocal_extraction",
    "primary_category": "vocal",
    "secondary_category": "vocal_extraction",
    "target_stem": "vocals",
    "supported": true,
    "unsupported_reason": "",
    "size_bytes": 123456789,
    "local": {
      "complete": true,
      "missing_count": 0
    },
    "remote": {
      "available": true,
      "source": "modelscope",
      "endpoint": null
    },
    "files": [
      {
        "role": "model",
        "relpath": "vocal/vocal_extraction/bs_roformer_voc_hyperacev2.ckpt",
        "exists": true,
        "size_bytes": 123456789,
        "remote_url": "https://www.modelscope.cn/models/baicai1145/pymss/resolve/master/vocal/vocal_extraction/bs_roformer_voc_hyperacev2.ckpt"
      }
    ]
  }
}
```

`files[].relpath` 是相对于 server 配置 model dir 的 catalog 相对路径。文件项包含角色、相对路径、本地存在性、本地大小和远程 URL；本地绝对路径保留在 server 内部。

catalog 中不存在该模型时，server 返回 `404 model_not_found`。

## POST /v1/models/load

`/v1/models/load` 加载模型。已有模型时，server 先关闭当前 `MSSeparator`，再加载目标模型。请求在加载完成或失败后返回；模型关闭和加载逻辑通过工作线程执行，`/health` 可在加载期间返回 `model_loading=true`。

请求示例：

```json
{
  "model": "bs_roformer_voc_hyperacev2",
  "source": "modelscope",
  "endpoint": null,
  "inference_params": {
    "batch_size": 2
  }
}
```

下表列出请求字段。`source` 和 `endpoint` 只作用于本次加载过程中的缺失文件自动下载。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `model` | string | 是 | catalog `name`、catalog alias 或 `model_registry` 支持的 stem |
| `source` | string | 否 | 下载源；省略时使用当前默认下载源 |
| `endpoint` | string/null | 否 | 下载 endpoint；省略时使用当前默认 endpoint，传 `null` 表示本次使用下载源默认 endpoint |
| `inference_params` | object | 否 | 本次加载模型的推理参数覆盖 |

响应示例：

```json
{
  "object": "model.load",
  "previous_model_loaded": false,
  "model_loaded": true,
  "model": {
    "id": "bs_roformer_voc_hyperacev2.ckpt",
    "object": "model",
    "created": 0,
    "owned_by": "pymss",
    "pymss": {
      "catalog_name": "bs_roformer_voc_hyperacev2.ckpt",
      "model_type": "bs_roformer",
      "architecture": "bs_roformer",
      "category": "vocal/vocal_extraction",
      "catalog_target_stem": "vocals",
      "supported": true,
      "sample_rate": 44100,
      "instruments": ["vocals", "instrument"],
      "instruments_source": "separator.config.training.instruments",
      "supported_parameters": {
        "inference": ["batch_size", "standardize", "normalize"]
      }
    }
  }
}
```

响应中的 `model.id` 始终是 catalog `name`。加载或切换期间，另一个加载请求和推理请求返回 `409 model_operation_in_progress`。显式下载正在进行时，加载请求返回 `409 model_download_in_progress`。

## POST /v1/models/download

`/v1/models/download` 下载单个 catalog 模型需要的全部文件到 server 配置的 model dir。该端点只执行文件下载；已加载模型状态保持不变。

请求示例：

```json
{
  "model": "bs_roformer_voc_hyperacev2",
  "source": "modelscope",
  "endpoint": null,
  "force": false,
  "verify": true,
  "timeout_seconds": 30
}
```

下表列出请求字段。`source` 和 `endpoint` 只作用于本次下载。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `model` | string | 是 | catalog `name`、alias 或 stem |
| `source` | string | 否 | 下载源；省略时使用当前默认下载源 |
| `endpoint` | string/null | 否 | 下载 endpoint；省略时使用当前默认 endpoint，传 `null` 表示本次使用下载源默认 endpoint |
| `force` | bool | 否 | 重新下载已存在文件，默认 `false` |
| `verify` | bool | 否 | 使用下载逻辑的大小和哈希校验，默认 `true` |
| `timeout_seconds` | number | 否 | 单次网络请求超时，默认 `30` |

响应示例：

```json
{
  "object": "model.download",
  "model": {
    "id": "bs_roformer_voc_hyperacev2.ckpt",
    "object": "pymss.model_catalog_entry",
    "owned_by": "pymss",
    "pymss": {
      "name": "bs_roformer_voc_hyperacev2.ckpt",
      "aliases": ["bs_roformer_voc_hyperacev2.ckpt", "bs_roformer_voc_hyperacev2"],
      "model_type": "bs_roformer",
      "architecture": "bs_roformer",
      "category": "vocal/vocal_extraction",
      "primary_category": "vocal",
      "secondary_category": "vocal_extraction",
      "target_stem": "vocals",
      "supported": true,
      "unsupported_reason": "",
      "size_bytes": 123456789,
      "local": {
        "complete": true,
        "missing_count": 0
      },
      "remote": {
        "available": true,
        "source": "modelscope",
        "endpoint": null
      }
    }
  },
  "source": "modelscope",
  "endpoint": null,
  "downloaded": ["vocal/vocal_extraction/bs_roformer_voc_hyperacev2.ckpt"],
  "skipped": ["vocal/vocal_extraction/bs_roformer_voc_hyperacev2.yaml"]
}
```

`downloaded` 和 `skipped` 是相对于 server 配置 model dir 的 catalog 相对路径。模型文件已完整存在且 `force=false` 时，响应状态码为 `200`，`downloaded=[]`，已有文件进入 `skipped`。

该端点允许下载 unsupported catalog model；`/v1/models/load` 在加载阶段按 `resolve_model(..., require_supported=True)` 校验模型推理支持状态。

行为与失败语义：

- 请求在下载完成或失败后返回。
- 下载逻辑通过工作线程执行，server 可继续处理其他请求。
- 请求中的 `source` 和 `endpoint` 只影响本次下载。
- 另一个显式下载正在进行时返回 `409 model_download_in_progress`。
- 模型加载或切换正在进行时返回 `409 model_operation_in_progress`。
- catalog 中不存在时返回 `404 model_not_found`。
- `model` 为空时返回 `400 invalid_model`。
- `source` 非法或 `endpoint` 类型非法时返回 `400 invalid_download_source`。
- 下载失败、大小校验失败、哈希校验失败或网络错误时返回 `500 model_download_failed`。

## GET /v1/download-source

`/v1/download-source` 返回当前默认下载源。该默认值用于后续 `/v1/models/download`，也用于 `/v1/models/load` 触发的缺失文件自动下载。

```json
{
  "object": "download.source",
  "source": "modelscope",
  "endpoint": null
}
```

## POST /v1/download-source

`POST /v1/download-source` 切换当前默认下载源。已加载模型保持原状态；新的默认值作用于后续下载和缺失文件自动下载。

请求示例：

```json
{
  "source": "huggingface",
  "endpoint": null
}
```

下表列出请求字段。`endpoint` 字段省略时保留当前 endpoint。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `source` | string | 是 | `modelscope`、`huggingface` 或 `hf-mirror` |
| `endpoint` | string/null | 否 | 自定义下载 endpoint；传 `null` 表示清空当前 endpoint |

响应与 `GET /v1/download-source` 一致。下载正在进行时返回 `409 model_download_in_progress`；模型加载或切换正在进行时返回 `409 model_operation_in_progress`。

## POST /v1/audio/separations

`/v1/audio/separations` 执行一次源分离。server 接受 JSON（JavaScript Object Notation，文本对象表示格式）请求和 PCM（Pulse-Code Modulation，脉冲编码调制）二进制请求。

下表展示两种输入传输方式。两种方式都以 PCM 字节和音频元数据作为协议边界；客户端负责读取音频来源、解码音频容器、重采样到模型 sample rate，并发送 PCM 数据。

| Content-Type | 音频位置 | 元数据位置 | 适用场景 |
|---|---|---|---|
| `application/json` | JSON `input.data` base64 | JSON 字段 | 调试、短音频、浏览器或脚本 |
| `application/octet-stream` | HTTP 请求体中的 raw PCM bytes | query 参数 | 长音频、生产调用或高吞吐调用 |

表中两种请求进入同一分离流程：server 校验模型、音频格式、采样率、声道数、stem 和输出格式，然后调用已加载的 `MSSeparator` 执行分离。

### JSON/base64 请求

```json
{
  "model": "bs_roformer_voc_hyperacev2.ckpt",
  "input": {
    "format": "pcm_f32le",
    "sample_rate": 44100,
    "channels": 2,
    "data": "base64-encoded-little-endian-pcm"
  },
  "stems": ["vocals"],
  "response_format": "json",
  "output_audio_format": "pcm_f32le"
}
```

下表列出 JSON 请求字段。`stems` 用于选择返回的 stem。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `model` | string | 是 | 当前已加载模型的 catalog `name` |
| `input.format` | string | 是 | `pcm_f32le` 或 `pcm_s16le` |
| `input.sample_rate` | integer | 是 | 必须等于模型 sample rate |
| `input.channels` | integer | 是 | `1` 或 `2` |
| `input.data` | string | 是 | base64 编码的 interleaved PCM bytes |
| `stems` | string[] 或 string | 否 | 要返回的 stem；省略或空值表示全部 |
| `response_format` | string | 否 | `json` 或 `zip`，默认 `json` |
| `output_audio_format` | string | 否 | `pcm_f32le`、`wav`、`flac`，默认 `pcm_f32le` |

`response_format=json` 与 `output_audio_format=pcm_f32le` 组合使用。`wav` 和 `flac` 输出音频格式用于 `response_format=zip`。

### 二进制 PCM 请求

```http
POST /v1/audio/separations?model=bs_roformer_voc_hyperacev2.ckpt&format=pcm_f32le&sample_rate=44100&channels=2&stems=vocals&response_format=zip&output_audio_format=wav
Content-Type: application/octet-stream

<raw interleaved little-endian PCM bytes>
```

下表列出 query 参数。`stems` 使用逗号分隔多个 stem。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `model` | string | 是 | 当前已加载模型的 catalog `name` |
| `format` | string | 是 | `pcm_f32le` 或 `pcm_s16le` |
| `sample_rate` | integer | 是 | 必须等于模型 sample rate |
| `channels` | integer | 是 | `1` 或 `2` |
| `stems` | string | 否 | 逗号分隔 stem；省略或空字符串表示全部 |
| `response_format` | string | 否 | `json` 或 `zip`，默认 `json` |
| `output_audio_format` | string | 否 | `pcm_f32le`、`wav`、`flac`，默认 `pcm_f32le` |

二进制 PCM 请求把音频字节放在 HTTP 请求体中，元数据由 query 参数提供。

## PCM 格式

server 接受两种 PCM 输入格式，并在内部转换为 `MSSeparator.separate()` 使用的 NumPy channel-first 数组。

`pcm_f32le`：

- little-endian（小端序）float32。
- 立体声传输布局为 interleaved（交错布局）：`L0, R0, L1, R1, ...`。
- 输入值必须全部为有限数。

`pcm_s16le`：

- little-endian signed int16。
- 解码后转换为 float32。

返回 `pcm_f32le` 时，server 将分离结果转换为 interleaved little-endian float32 bytes。

## stems 行为

`stems` 控制响应中返回哪些分离音轨。server 使用已加载模型的 `pymss.instruments` 作为合法 stem 集合。

- `stems` 省略、空字符串、空数组或只包含空白项时，返回模型配置中的全部 instruments。
- stem 名称大小写不敏感；响应使用模型配置里的原始名称。
- 请求不存在的 stem 返回 `400 invalid_stem`。
- 指定 `stems` 时响应按请求顺序排列；省略时按模型 instruments 顺序排列。
- VR（Vocal Remover，pymss 的人声移除模型类型）模型执行完整 VR 推理，HTTP 层按请求的 `stems` 过滤响应内容。

## JSON 响应

当 `response_format=json` 时，响应中的音频始终是 base64 编码的 `pcm_f32le` bytes。

```json
{
  "id": "sep_5f16f0e383f948229f85eadd98ac2c64",
  "object": "audio.separation",
  "created": 1760000000,
  "model": "bs_roformer_voc_hyperacev2.ckpt",
  "outputs": [
    {
      "stem": "vocals",
      "audio": {
        "format": "pcm_f32le",
        "sample_rate": 44100,
        "channels": 2,
        "data": "base64-encoded-little-endian-pcm"
      }
    }
  ],
  "metadata": {
    "input_seconds": 30.0,
    "output_stems": ["vocals"],
    "device": "cuda:0"
  },
  "usage": {
    "type": "duration",
    "seconds": 30.0
  }
}
```

`outputs` 按 stem 顺序排列。`metadata.input_seconds` 和 `usage.seconds` 均来自解码后的输入 PCM 时长。

## ZIP 归档响应

当 `response_format=zip` 时，响应体是 ZIP（ZIP archive，压缩归档格式）文件，`Content-Type` 为 `application/zip`。每个 stem 对应一个音频文件，归档中包含 `manifest.json`。

下面的示例展示 ZIP entry（归档条目）文件名：

```text
manifest.json
0001-vocals.wav
0002-instrument.wav
```

音频文件名由 server 生成，格式为 `0001-{stem}.{extension}`。`manifest.json` 描述每个输出 stem 对应的文件名、格式、sample rate 和声道数。

`manifest.json` 示例：

```json
{
  "id": "sep_5f16f0e383f948229f85eadd98ac2c64",
  "object": "audio.separation",
  "model": "bs_roformer_voc_hyperacev2.ckpt",
  "outputs": [
    {
      "stem": "vocals",
      "filename": "0001-vocals.wav",
      "format": "wav",
      "sample_rate": 44100,
      "channels": 2
    }
  ],
  "usage": {
    "type": "duration",
    "seconds": 30.0
  }
}
```

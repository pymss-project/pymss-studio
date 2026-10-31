# pymss Server CLI

pymss server 通过 CLI（Command-Line Interface，命令行接口）命令 `pymss serve [MODEL]` 启动。`MODEL` 是可选位置参数，取值为 catalog（模型目录）模型名或 alias。省略 `MODEL` 时，server 以空载状态监听 HTTP（HyperText Transfer Protocol，超文本传输协议）端口，并可通过 HTTP API（Application Programming Interface，应用程序接口）加载模型。

## 安装可选依赖

server 依赖位于 `server` extra。使用 uv 安装依赖：

```bash
uv sync --extra server
```

使用 pip 安装依赖：

```bash
pip install "pymss[server]"
```

## 启动命令

下面的命令启动 server，并在启动阶段加载 `bs_roformer_voc_hyperacev2`：

```bash
uv run --extra server pymss serve bs_roformer_voc_hyperacev2 \
  --host 127.0.0.1 \
  --port 8000 \
  --device auto \
  --source modelscope
```

启动成功后，server 监听 `127.0.0.1:8000`，并使用 `modelscope` 作为默认下载源。

## 启动行为

提供 `MODEL` 时，server 在监听 HTTP 端口前完成模型准备和加载。启动顺序如下：

1. 调用 `resolve_model(MODEL, require_supported=True, require_exists=True)` 检查 catalog 和本地文件。
2. 本地文件缺失且异常为 `FileNotFoundError` 时，调用 `download_model()` 下载模型主文件、配置文件和辅助文件。
3. 下载完成后再次调用 `resolve_model(..., require_exists=True)`。
4. 创建唯一的 `MSSeparator`（pymss 的模型推理封装）实例。
5. 模型加载成功后监听 HTTP 端口。

下载失败或模型加载失败会使启动命令退出，HTTP 端口保持未监听状态。

省略 `MODEL` 时，server 不创建 `MSSeparator` 实例，并直接监听 HTTP 端口。该状态下，`/health` 返回 `model_loaded=false`，`/v1/models` 返回空列表，推理请求返回 `503 model_not_loaded`。客户端可通过 `POST /v1/models/load` 加载模型。

运行中的 server 可通过 `POST /v1/download-source` 切换默认下载源。该设置影响后续 `POST /v1/models/download`，以及 `/v1/models/load` 在本地缺少文件时触发的自动下载。已加载模型保持原状态。

## CLI 参数

全局参数写在子命令名前。当前 server 文档只涉及 `--model-dir`：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--model-dir` | pymss 默认模型目录 | 模型缓存目录；也可通过 `PYMSS_MODEL_DIR` 控制 |

`serve` 子命令参数如下：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `MODEL` | 空 | catalog 模型名或 alias；省略时空载启动 |
| `--source` | `modelscope` | 下载源：`modelscope`、`huggingface`、`hf-mirror` |
| `--endpoint` | 空 | 自定义下载 endpoint；该 endpoint 按 catalog 相对路径提供文件 |
| `--device` | `auto` | 推理设备：`auto`、`cpu`、`cuda`、`mps`、`mlx` |
| `--device-id` | `0` | CUDA（Compute Unified Device Architecture，NVIDIA GPU 计算平台）device id，可重复 |
| `--host` | `127.0.0.1` | HTTP 监听地址 |
| `--port` | `8000` | HTTP 监听端口 |
| `--api-key` | 空 | 可选 Bearer token（Bearer 令牌）；设置后 `/v1/*` 需要鉴权 |
| `--debug` | `false` | 启用 debug 日志 |
| `--param` | 空 | 推理参数覆盖，格式为 `key=value`，可重复 |
| `--max-audio-seconds` | `600.0` | 单请求 decoded PCM 秒数上限；`0` 表示不限制 |
| `--max-request-bytes` | `536870912` | HTTP body 大小上限，默认 512 MiB（Mebibyte，二进制兆字节） |
| `--max-queue-size` | `8` | 正在处理和等待处理的请求数量上限 |
| `--request-timeout-seconds` | `0.0` | 单请求推理超时；`0` 表示不限制 |
| `--webui` | `false` | 启用 WebUI（Web User Interface，浏览器用户界面），路径为 `/ui/` |

`--param` 的值会进入 `ServerConfig.inference_params`。通过 `POST /v1/models/load` 传入的 `inference_params` 会覆盖同名启动参数。

## WebUI

`--webui` 在当前 server 上启用同源浏览器操作台。启动命令如下：

```bash
uv run --extra server pymss serve --webui --host 127.0.0.1 --port 8000
```

启动成功后，uvicorn 日志输出 WebUI 地址：

```text
INFO:     WebUI available at http://127.0.0.1:8000/ui/
```

浏览器访问地址如下：

```text
http://127.0.0.1:8000/ui/
```

监听地址为 `0.0.0.0` 或 `::` 时，日志中的 WebUI 地址使用 `127.0.0.1`，便于本机浏览器访问。IPv6（Internet Protocol version 6，互联网协议第 6 版）地址会使用方括号格式，例如 `http://[::1]:8000/ui/`。

WebUI 调用同源 HTTP API。用户音频由浏览器读取、解码和重采样后发送 PCM（Pulse-Code Modulation，脉冲编码调制）bytes。设置 `--api-key` 时，WebUI 页面可加载；页面中的 `/v1/*` 请求需要用户输入 Bearer token。

## 测试脚本环境变量

`test/server/start_server.sh` 用于启动测试 server。脚本读取下表中的环境变量，并把额外命令行参数原样传给 `pymss serve`。

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `MODEL` | `bs_roformer_voc_hyperacev2` | 启动模型；设置为显式空值 `MODEL=` 时空载启动 |
| `HOST` | `127.0.0.1` | HTTP 监听地址 |
| `PORT` | `8000` | HTTP 监听端口 |
| `DEVICE` | `auto` | 推理设备 |
| `SOURCE` | `modelscope` | 下载源 |
| `MODEL_DIR` | 空 | 模型缓存目录 |
| `ENDPOINT` | 空 | 自定义下载 endpoint |
| `API_KEY` | 空 | Bearer token |

示例命令设置端口和 API key：

```bash
PORT=8010 API_KEY=test-token ./test/server/start_server.sh
```

示例命令传递 server 限制参数：

```bash
./test/server/start_server.sh --max-audio-seconds 120 --request-timeout-seconds 60
```

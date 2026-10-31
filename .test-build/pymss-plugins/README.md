# pymss 插件

pymss 与 pymss Studio 的官方插件源码集合。这里集中维护官方插件；第三方插件采用一个插件一个独立 Git 仓库，用户通过仓库链接安装。

## 开发新插件，从这里开始

**新建自己的插件仓库，把 [`examples/audio_gain`](examples/audio_gain/README.zh-CN.md) 的内容复制到仓库根目录。** 模板带有 Python 后端、Vue 界面、独立中英文词典和构建配置。

1. 创建独立仓库，例如 `alice/pymss-gain`，根目录直接放置 `pyproject.toml`、`backend/`、`ui/` 等模板文件。
2. 修改 `pyproject.toml` 中的插件 ID、Operation ID、名称和描述，同步修改后端注册与界面调用的 ID。
3. 在插件目录内安装前端依赖并构建：

   ```sh
   pnpm install --frozen-lockfile
   pnpm build
   ```

4. 提交源码、`ui/dist/index.html` 和许可文件。用户在 Studio 的「插件 → 从来源安装」中填写仓库链接，例如 `https://github.com/alice/pymss-gain`。

本机开发时可先选择本地仓库目录安装；后续修改界面需要重新构建，再更新安装副本。只有准备向官方源码集合提交 PR 时，才需要 Fork 本仓库并把插件放入 `plugins/`。

公开的 HTTPS GitHub 仓库无需用户安装 Git，默认获取默认分支的最新代码。插件在自己的 `pyproject.toml` 中声明 SDK 版本、必需接口和实际存在的平台限制，Studio 会提前检查兼容性。模板构建需要 Node.js、pnpm 和 Python 3.11+；安装已发布插件不需要前端构建工具。

开发者主要编辑这些文件：

```text
你的独立插件仓库/
├── pyproject.toml       插件身份、依赖、Operation 和界面声明
├── __init__.py          插件入口
├── backend/             Python 处理逻辑
├── ui/
│   ├── src/             界面；locales/*.json 维护本插件的翻译
│   └── dist/index.html  构建后的安装页面
├── package.json         本插件的前端依赖与构建命令
└── README.md            使用说明
```

详细步骤见[模板说明](examples/audio_gain/README.zh-CN.md)，接口和发布约定见[插件开发指南](docs/development.md)。独立插件仓库不需要本仓库的 `plugins/`、`examples/`、`tooling/` 或 `registry.json`。通过链接安装不要求先被官方目录收录。

官方插件与模板都使用包内 `ui/src/locales/`：`en.json` 为默认词典，`zh-CN.json` 为中文词典，新增语言文件会自动参与构建。插件名称和市场描述则在本插件 `pyproject.toml` 的 `tool.pymss.studio.locales` 中维护。

## 仓库目录

| 目录或文件 | 用途 |
| --- | --- |
| [`plugins/`](plugins/) | 正式插件，每个子目录可独立安装 |
| [`examples/`](examples/) | 开发模板，不进入正式插件目录 |
| [`docs/`](docs/) | 开发约定、SDK 和仓库维护说明 |
| [`tooling/`](tooling/README.md) | 官方仓库维护工具：共享界面、构建脚本、验证文件和 Node 配置 |
| `registry.json` | 自动生成的插件目录，供 pymss 和 Studio 读取 |
| `LICENSE` | 仓库许可证；各插件附带自己的许可说明 |

## 官方插件

| 目录 | 功能 |
| --- | --- |
| `convert` | 音频格式、采样率和声道转换 |
| `inspect` | 音频信息与元数据 |
| `sdr` | SDR / SI-SDR 分离质量评估 |
| `slicer` | 静音检测与音频切片 |
| `merge` | 多文件音频合并 |
| `loudnorm` | 响度测量与匹配 |
| `asr` | 语音识别与字幕导出 |
| `game` | 歌声转 MIDI |
| `tempo_key` | BPM 与调性检测 |
| `analysis` | 响度、峰值、RMS 和频谱分析 |
| `dynamics` | 压缩、限幅和去齿音 |
| `spectral` | 均衡与滤波 |
| `timing` | 音高与速度调整 |

用户可在 Studio 插件市场安装，也可通过「从来源安装」选择 `plugins/` 下的单个插件目录。pymss 命令行同样支持本地目录：

```sh
pymss install ./plugins/loudnorm
```

## 维护官方仓库

维护现有官方插件的共享界面或发布目录时，使用集中在 `tooling/` 的工具：

```sh
pnpm --dir tooling install --frozen-lockfile
pnpm --dir tooling build
```

`registry.json` 由插件声明生成，不手工编辑。每个插件的 `ui/dist/index.html` 和第三方许可说明需要随源码提交，Studio 安装时直接使用这些产物。

更多维护命令、SDK 接口和发布流程见[仓库参考](docs/repository.md)。

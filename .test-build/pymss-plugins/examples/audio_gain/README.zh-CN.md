# 音频增益插件开发参考

本目录提供完整、可独立构建的工具插件：Python Operation 负责真实音频处理，Vue 页面负责交互，每个插件维护自己的中英文词典。开发时将本目录的内容复制到新仓库根目录，按“一个插件一个仓库”的方式发布；无需 Fork 整个官方仓库。本模板位于 `examples/`，不会自动进入正式插件市场。

## 目录与职责

```text
pyproject.toml             插件ID、版本、依赖、视图、分类和权限
__init__.py                插件加载入口
backend/gain.py            Python 处理实现
ui/src/App.vue             交互页面
ui/src/locales/            该插件自己的中英文界面文案
ui/src/studio.ts           宿主SDK类型与调用封装
ui/dist/index.html         随插件发布的编译页面
scripts/build.mjs          独立构建与产物校验
tests/                    处理逻辑和包结构测试
```

`pyproject.toml` 中的 `locales` 用于市场卡片和工具导航；`ui/src/locales/` 用于页面内部。两部分由插件作者维护，Studio 只发送当前语言。此插件不导入官方仓库的全局词典、公共前端源码或 Studio 源码。

新增语言时，在 `ui/src/locales/` 添加以语言代码命名的 JSON 文件，例如 `ja.json`，构建时会自动收录。`en.json` 是必需的默认词典。匹配顺序为完整语言代码、基础语言、同语言变体、英文；缺少单条翻译时也回退英文。Naive UI 控件当前配置中文和英文，新增语言时可在 `App.vue` 配置对应的组件库 locale。

## 构建与安装

需要 Node.js 22.12 或以上版本、pnpm 10.33.2 和 Python 3.11+（构建时读取 `pyproject.toml`）：

```sh
pnpm install --frozen-lockfile
pnpm build
```

提交源码时同时提交 `ui/dist/index.html` 和 `THIRD-PARTY-NOTICES.txt`。执行 `node scripts/build.mjs --check` 可检查产物是否与源码一致。Python 源码直接运行，不存在复制生成后端的步骤。

本机开发时，在 Studio 的“插件 → 从来源安装 → 选择目录”中选择本目录；发布到独立仓库后，用户直接在“从来源安装”中填写仓库链接。安装后进入“工具 → 编辑 → 音频增益”，选择输入和输出目录即可运行。网页单独打开时没有宿主服务，不能调用文件选择与处理接口。

峰值保护默认开启，必要时按比例降低整体增益；它不采用额外的限幅器算法。输入文件不被修改，采样率和声道布局保持不变。

安装兼容版本的 pymss 和 NumPy 后，可执行：

```sh
python -m unittest discover -s tests -v
```

在官方仓库检验与 Studio 的配合时，还可以运行：

```sh
python tooling/tests/manual/reference_plugin_view_smoke.py --studio-root ../pymss-studio
python tooling/tests/manual/reference_plugin_install_smoke.py --studio-root ../pymss-studio
```

前者使用 Studio 实际提供的 SDK 和受控宿主事件检查界面；后者在临时插件目录安装、处理真实 WAV、更新、禁用、启用和卸载，不改变本机已安装插件，也不下载模型。

CLI 可通过 `pymss install /path/to/audio_gain` 安装已经构建好的目录。Operation ID 为 `example.audio_gain.apply`，输入为音频 Artifact，参数为 `gain_db`、`protect_peak`，输出为音频 Artifact 和结构化报告；执行处理不需要加载界面。

## 如何改成自己的插件

1. 新建独立 Git 仓库，将本目录的内容复制到仓库根目录。复制后可以独立安装前端依赖并构建。
2. 仓库和安装目录使用带作者前缀的名称，避免同名冲突；替换 `pyproject.toml`、`backend/gain.py`、`ui/src/studio.ts` 中的插件和 Operation ID。发布后保持插件 ID 稳定。
3. 修改包名称、描述、分类、权限和说明，替换后端与页面，在本插件的 `locales/` 中维护文案。
4. 验证参数、输出、错误、语言/主题切换、进度、取消、保存，以及安装、更新、禁用和卸载。
5. 增加插件版本，提交并发布源码和编译页面，再发布目录信息或安装地址。

`[tool.pymss.studio] build_script = "scripts/build.mjs"` 表示插件拥有独立构建器。贡献到官方仓库后，批量构建会调用该脚本；运行时的 Studio 不执行这个字段，也不在安装时运行 Node 构建。

## 两种发布方式

- **独立仓库发布（默认）**：仓库根目录保留完整插件包，用户通过“从来源安装”填写仓库链接或选择本地目录。公开的 HTTPS GitHub 仓库通过源码归档下载，不需要 Git；默认跟随默认分支的最新提交，也可使用 `https://github.com/author/repository@v0.1.0` 选择标签版本。Studio 记录来源和实际 commit，后续更新能够识别代码变化。其他 Git 来源仍需要本机 Git。
- **官方源码贡献**：希望由官方共同维护时，Fork 官方仓库，放到 `plugins/<唯一名称>/` 后提交 PR。通过审核后由生成器写入官方目录。不要手工修改生成的 `registry.json`。

当前尚未实现多源市场，以及将任意外部仓库自动收录到官方目录。独立发布不会自动获得官方市场展示。

## SDK 与兼容边界

页面通过 Studio 提供的 `pymss-sdk.js` 通信，不直接调用 Tauri。例子包含：连接和超时处理、文件选择、偏好设置、独立语言切换、主题跟随、进度、取消、结果和日志弹窗。取消请求返回后仍等待任务终态，避免提前将运行任务当作已经停止。

`[tool.pymss.studio]` 声明 `sdk_version = 1` 和页面使用的完整 `required_capabilities`。构建器从 `pyproject.toml` 注入要求，连接时检查 `appInfo()` 返回的 `sdkVersion` 和 `capabilities`，通过后才读取设置。SDK 主版本独立于 Python 插件的 `api_version = 2`。

视图 `permissions` 授予调用权限，`required_capabilities` 确认宿主实现了服务；能力名称必须是 `operations.start` 这样的完整名称，不能使用 `operations` 权限族名称替代。

只有插件存在实际限制时才声明 `platforms`（`win32`、`linux`、`darwin`）、`architectures`（`x86_64`、`arm64`）或 `backends`（`torch`、`mlx`）；省略或空数组表示不限制。`backends` 表示当前运行环境至少具备其中一个基础框架，不代表 GPU、模型或可选组件已就绪。本插件只使用 NumPy，不需要这些限制。新的宿主接口仍需要 Studio 更新。Python 插件执行本地代码，界面的 iframe 限制不等于 Python 沙箱。

安装和更新比较会跳过 `node_modules`、虚拟环境、开发缓存以及 `.env` 文件。模型、凭据和用户输出应放在插件源码目录之外。

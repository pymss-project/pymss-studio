# 官方仓库维护工具

这里集中存放构建和维护官方插件所需的文件。开发独立插件请从 [`examples/audio_gain`](../examples/audio_gain/README.zh-CN.md) 开始。

```text
tooling/
├── frontend/       官方插件共用的界面组件与 SDK 适配
├── scripts/        页面构建、插件入口读取和目录生成
├── tests/          Python、前端及集成验证文件
├── catalog.toml    官方插件目录的源码地址与版本配置
├── package.json    维护命令和共享前端依赖
└── pnpm-lock.yaml  依赖锁文件
```

以下命令从仓库根目录运行：

```sh
pnpm --dir tooling install --frozen-lockfile
pnpm --dir tooling build
pnpm --dir tooling build:tools --tool=game
pnpm --dir tooling catalog
```

`build` 执行前端类型检查、生成各插件页面并更新根目录的 `registry.json`。单独维护某个插件时可以通过 `--tool` 限定页面构建范围。

页面词典由各插件的 `ui/src/locales/*.json` 提供，构建器自动加载当前插件的语言文件。这里不维护全局页面词典。

验证入口保留在维护目录，按需运行：

```sh
pnpm --dir tooling test:frontend
pnpm --dir tooling test:python
```

模板拥有独立依赖，请先在 `examples/audio_gain/` 内安装依赖，再使用它自己的 `pnpm build`。共享构建工具不向独立插件注入依赖。

本地 `node_modules/`、缓存和 `.tmp/` 均不提交。完整接口和发布约定见[插件开发指南](../docs/development.md)及[仓库参考](../docs/repository.md)。

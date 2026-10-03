---
name: hello-plugin-usage
description: hello-plugin 示例插件的使用说明：/hello 命令、plugin_hello 工具与插件开发入口
---

# hello-plugin 使用说明

本技能由插件目录 `data/plugins/hello-plugin/skills/` 自动并入技能系统，
演示"插件自带技能"的装载方式（项目级 `.bawcode/skills` 同名仍最优先）。

## 能力

- 命令 `/hello <名字>`：本地回显问候，不消耗 LLM。
- 工具 `plugin_hello`：模型可调用的示例工具，返回一行问候文本。
- 上下文补充：在 `config.json` 配置 `plugins_config.hello-plugin.supplement=true` 后，
  每回合自动注入一行 `[插件补充]` 文本。

## 二次开发

复制 `data/plugins/hello-plugin/` 整个目录到 `data/plugins/<新id>/`，改 `plugin.json`
的 `id` 与入口 `main.py`，执行 `/plugin reload` 即可生效。完整 API 见
`docs/plugin-development.md`。

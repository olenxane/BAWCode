---
name: plugin-creator
description: 创建/开发 BAWCode 插件时使用：从骨架模板 scaffold 新插件并按需扩展 hooks/commands/tools/交互面板/后台线程。当用户要求"写一个插件""做个插件""给 BAWCode 加功能"或改造现有插件时触发
---

# BAWCode 插件创建与开发

## 流程

### 1. 确定需求形态

先与用户确认插件要做什么，归入以下一类或几类（决定注册哪些扩展点）：

| 需求 | 扩展点 |
|---|---|
| 给模型一个新能力（模型自主调用） | `ctx.register_tool` |
| 给用户一个斜杠命令 | `ctx.register_command` |
| 每回合注入上下文/提示 | `ctx.register_context_supplement` |
| 观察或改写生命周期事件 | `ctx.register_hook` |
| 与用户交互（卡片/选择/输入） | `ctx.ui.ask / choose / line / confirm` |
| 后台常驻任务（监听/轮询/服务） | `ctx.spawn_worker` + `register_teardown` |
| 需要配置项 | plugin.json `config` 声明数组 |

### 2. Scaffold 骨架

1. 与用户确认插件 id（字母数字开头，允许 `-_`）、名称、一句话描述
2. 放置位置二选一（拿不准就问用户）：
   - 全局：`data/plugins/<id>/`（所有项目可用）
   - 项目级：`{工作区}/.bawcode/plugins/<id>/`（仅当前项目，覆盖同名全局插件）
3. 复制模板两份文件到目标目录并替换占位符：
   - `templates/plugin.json`：`{{PLUGIN_ID}}` `{{PLUGIN_NAME}}` `{{PLUGIN_DESC}}`
   - `templates/main.py`：`{{PLUGIN_ID}}` `{{PLUGIN_CMD}}`（命令名，取 id 主体去掉连字符或缩写）
4. 骨架自带的 `/hello` 风格示例命令仅用于冒烟验证——确认能装载后**应立即按真实需求改写或删除**

### 3. 按需求扩展实现

完整 API 参考在 `references/plugin-development.md`（用 read 查看，必要时分段读）。关键点速查：

- 工具 `schema` 是 JSON Schema，形参名与属性名一致并给缺省值；`safe=True` 仅限纯交互无副作用工具
- hook 事件名建议引用常量：`from bawcode_sdk.events import AFTER_TURN` 等；目录外事件会 warn（自定义互通事件属预期可忽略）
- `ctx.ui.*` 只能在 agent/后台线程调用，主线程命令 handler 里会抛 RuntimeError
- 后台线程一律 `ctx.spawn_worker`（卸载自动兜底），停止信号用 `ctx.current_worker.stop_event` 或闭包 stop_event
- 持久化：`ctx.storage_dir()`（项目级）或 `ctx.storage_dir(scope="global")`（跨项目共享，无跨实例锁）
- 配置读取用 `ctx.settings` 实时视图（处理函数内读，勿 setup 时缓存）
- 需要第三方库时写 `requirements.txt`（宿主自动安装），import 失败要自行降级

### 4. 验证（必须真实运行）

1. 语法：`python -m compileall -q <插件目录>`
2. 装载：让用户执行 `/plugin reload`（或重启），`/plugin` 应显示 `[装载]`；失败会带原因
3. 冒烟：调用命令/工具验证真实行为；观察型插件触发对应事件验证
4. 重载洁净：`/plugin reload` 两次，确认无残留线程/端口/重复注册
5. 交付前自查：无调试 print、无硬编码个人路径、plugin.json 的 version/description 已填写

## 禁区

- 不要 import `core.*` 宿主内部模块——能力面都在 `bawcode_sdk`（`ctx.*` 与 `ctx.host.*`）；SDK 没有的能力先询问用户而不是绕行
- 不要把 `write`/`execute_command` 类副作用工具名声明为 safe
- 不要在主线程调用 `ctx.ui.*`
- 不要在 setup 里做耗时操作（网络请求/重计算）——装载在主线程，会卡住启动

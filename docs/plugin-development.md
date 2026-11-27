# BAWCode 插件开发指南

插件系统把 **hook 扩展点、斜杠命令、模型可见工具、技能 Skills、上下文补充、类用户操作接口** 打包为一个可分发的目录。类用户操作接口涵盖发消息、通知和交互面板。
插件放进插件目录即可生效，无需改动主程序代码。

- 插件 = 一个目录：`plugin.json` 清单 + `main.py` 入口 + 可选的 `skills/` 等资源
- 装载时机：BAWCode 启动时自动发现；无需等待依赖的插件先于会话初始化装载，可收到首次 `session_start`，后台补装插件在依赖完成后装载
- 官方完整示例：`data/plugins/hello-plugin/`，包含命令、工具、hook、技能与配置示例；该内置示例默认禁用，先执行 `/plugin enable hello-plugin`

---

## 目录

1. [快速开始](#1-快速开始)
2. [目录结构与发现规则](#2-目录结构与发现规则)
3. [plugin.json 清单参考](#3-pluginjson-清单参考)
4. [PluginContext API 参考](#4-plugincontext-api-参考)
5. [Hook 事件目录](#5-hook-事件目录)
6. [斜杠命令与参数补全](#6-斜杠命令与参数补全)
7. [模型可见工具](#7-模型可见工具)
8. [插件自带技能](#8-插件自带技能)
9. [配置参考](#9-配置参考)
10. [/plugin 命令](#10-plugin-命令)
11. [调试](#11-调试)
12. [生命周期与线程模型](#12-生命周期与线程模型)
13. [兼容性：旧扩展方式](#13-兼容性旧扩展方式)
14. [FAQ](#14-faq)

---

## 1. 快速开始

**第 1 步**：创建目录 `data/plugins/my-first-plugin/`，放入两个文件。

`plugin.json`：

```json
{
  "id": "my-first-plugin",
  "sdk_version": 1,
  "name": "My First Plugin",
  "version": "0.1.0",
  "description": "演示：一条命令 + 一个 hook"
}
```

`main.py`：

```python
from bawcode_sdk import PluginContext

def setup(ctx: PluginContext):
    # 一条斜杠命令
    ctx.register_command(
        "/ping-me",
        hint="示例：返回 pong",
        handler=lambda cctx, args: f"pong{(' · ' + args) if args else ''}",
    )

    # 一个 hook：每回合结束收到通知
    def _after_turn(payload):
        ctx.log.info("回合结束: %s", (payload.get("user_text") or "")[:40])

    ctx.register_hook("after_turn", _after_turn)
```

**第 2 步**：启动 BAWCode；如果已经在运行，也可以执行 `/plugin reload`。输入 `/ping-me` 验证；
`/plugin` 可看到插件状态为 `[装载]`。

---

## 2. 目录结构与发现规则

### 2.1 插件目录：双层结构，与技能同范式

| 层级 | 位置 | 说明 |
|------|------|------|
| 全局 | `data/plugins/<插件目录>/` | 所有项目可用；可用 `config.plugins.dir` 改到别处 |
| 项目 | `{工作区}/.bawcode/plugins/<插件目录>/` | 仅当前项目可用；**同名插件覆盖全局版** |

覆盖规则：两个层级出现**相同 `id`** 的插件时，只装载项目级那份，日志会提示覆盖。
临时或私有辅助目录若以下划线或点开头，例如 `_drafts/`，会被跳过。

### 2.2 插件目录内部结构

```
my-plugin/
├── plugin.json        # 清单（推荐但非必需，见 §3）
├── main.py            # 入口：定义 setup(ctx)（entry 可指定其他文件名）
├── requirements.txt   # 可选：插件独有依赖，装载时缺失项自动补齐（见 §2.3）
├── skills/            # 可选：插件自带技能，自动并入技能系统（见 §8）
│   └── my-usage/
│       └── SKILL.md
├── lib/               # 可选：插件自己的辅助模块（使用 from .lib import helpers 相对导入）
└── （其他任意资源文件）
```

最小要求：目录里有 `main.py` 即可装载。此时清单字段全部取缺省值，`id` 取目录名。
**没有 `main.py` 且没有 `plugin.json` 的目录会装载失败**，状态显示为 `[失败]`，不影响其他插件。

### 2.3 插件依赖：requirements.txt

插件的第三方依赖**不随主程序安装**——请在插件目录放一份 `requirements.txt`，采用标准 PEP 508
格式，每行一个依赖，支持版本约束与 `#` 注释。宿主会在**首次装载该插件时**检查并按需
静默 `pip install -r requirements.txt` 补齐缺失项。TUI 下缺失依赖在后台安装，插件先显示
`[安装中]`，此时尚未导入入口或执行 `setup`，安装结束后由主线程自动补装载；无头模式仍同步安装：

```
# requirements.txt 示例
websockets>=13
Pillow>=10
```

规则与注意：

- **只检查、只安装缺失项**：依赖已满足时零开销跳过，不调用 pip；版本不满足也视为缺失并补装，
  例如已装 `websockets 12` 而要求 `>=13`
- 安装使用**当前运行 BAWCode 的解释器**，即 `sys.executable -m pip`，装进同一环境；完成后
  复核，个别仍不满足的记入 `/plugin` 诊断
- **失败不阻断装载**：pip 因断网、无 pip 等原因失败时只写日志，日志位于 `data/log`，命名空间为 `plugins`。
  安装结束后仍会尝试 `import + setup`——依赖可选的插件应自行 `try/except ImportError` 降级，
  例如自带工具缺依赖时不注册，或让命令给出缺失提示
- 关闭自动安装：`config.plugins.auto_install_deps = false`。此时仅检查，缺依赖状态显示在
  `/plugin` 的"未自动装"标注里，需用户自行安装
- 插件依赖请在 `requirements.txt` 声明，**不要**写进主程序根目录的 `requirements.txt`
- **跨插件冲突检测**：装载时若两个插件约束同一库且不可共存，或已装版本不满足其中一方，
  `/plugin` 行内会标注 `依赖冲突×N` 并附明细；纯检测不阻断装载，安装行为不变
- TUI 后台安装不阻塞启动，底部状态栏显示进度，完成后由帧循环补装载；无头模式的同步安装会阻塞
  启动或装载，下载大型包时尤其明显

---

## 3. plugin.json 清单参考

```json
{
  "id": "my-plugin",
  "name": "My Plugin",
  "version": "1.2.0",
  "description": "一句话说明插件做什么",
  "author": "your-name",
  "entry": "main.py",
  "enabled": true
}
```

| 字段 | 类型 | 缺省 | 说明 |
|------|------|------|------|
| `id` | string | 目录名 | 插件唯一标识。**装载后不可与他人重复**；只允许字母、数字、`-`、`_`，且以字母或数字开头。作为所有注册项的 owner，用于卸载与诊断 |
| `name` | string | 取 `id` | 展示名 |
| `sdk_version` | int | 省略为旧插件兼容模式 | 新插件填写 `1`；不支持的版本在导入前拒绝。SDK v1 使用独立包命名空间，不修改 sys.path |
| `version` | string | `"0.0.0"` | 版本号，`/plugin` 列表展示 |
| `description` | string | `""` | 一句话描述，`/plugin` 列表展示 |
| `author` | string | `""` | 作者 |
| `entry` | string | `"main.py"` | 入口文件名，相对插件目录。文件必须存在且能被 Python 导入 |
| `enabled` | bool | `true` | 清单级开关。`false` 时不装载，状态为 `[禁用]`。运行期启停用 `/plugin enable/disable`，优先级见下 |

**启用优先级**：从高到低排列，任一命中即不装载：

1. `config.plugins.enabled = false` —— 插件系统全局关闭
2. 插件 `id` 出现在 `config.plugins.disable` 列表 —— 单插件禁用，`/plugin disable` 会写入该列表
3. 清单 `enabled: false`

### 3.1 内置非必要插件默认禁用

六个内置非必要插件默认列入 `config.plugins.disable`：`music-player`、`computer-use`、`qbridge`、
`remote-control`、`hello-plugin`、`bottom-banter`。它们不会装载，也不会检查或安装依赖，
`/plugin` 中显示为 `[禁用]`，不是装载失败。使用前执行 `/plugin enable <id>`，命令会写回配置并重载。

---

## 4. PluginContext API 参考

`setup(ctx)` 收到的 `ctx` 是 `bawcode_sdk.PluginContext` 实例，这是插件的全部能力面。
所有经 ctx 注册的内容自动携带 `owner=插件 id`，插件卸载/重载时**整体注销**，无需手工清理。

### 4.1 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `ctx.plugin_id` | str | 插件 id |
| `ctx.dir` | Path | 插件目录绝对路径 |
| `ctx.manifest` | dict | 解析后的清单，含缺省补全 |
| `ctx.source` | str | `"global"` 或 `"project"` |
| `ctx.config` | Config | 宿主配置对象，遵循**只读约定**：读模型名、主题等可以，改配置请走 `/settings` |
| `ctx.workspace` | Path | 当前工作区根目录 |
| `ctx.settings` | dict | 插件配置**实时视图**，由声明缺省与 `config.plugins_config[<id>]` 持久化值组成。每次访问重读宿主配置——设置面板改动即时生效；动态行为请在处理函数内读取，勿在 setup 时缓存 |
| `ctx.log` | Logger | 命名空间化日志器，命名空间为 `plugins.<id>`，落盘到 `data/log/` |
| `ctx.ui` | PluginUI | 交互面板桥，提供 confirm/choose/line/ask，**仅 agent/后台线程可用**，见 §4.4 |
| `ctx.cancelled` | Event 或 None | 当前线程的回合取消令牌：工具执行期宿主已挂好，长循环或可切片工作按 `cancelled.is_set()` 及时退出；None 表示脱离回合，即后台独立线程，按不可取消处理 |
| `ctx.turn_active` | bool | 是否有回合进行中，只读，用于替代裸读 `runner.busy` |

### 4.2 注册方法

| 方法 | 用途 | 详细 |
|------|------|------|
| `ctx.register_hook(event, fn=None, priority=100, name="")` | 注册 hook 处理函数，也可作装饰器 | §5 |
| `ctx.register_user_message_supplement(fn, priority=100)` | 用户消息写入时焊入补充文本 | §5.3 |
| `ctx.register_external_api(event, fn=None, url="", priority=50)` | 实现 external_apis 同名扩展点 | §5.4 |
| `ctx.register_command(name, hint="", usage="", aliases=None, handler=None, completer=None)` | 注册斜杠命令 | §6 |
| `ctx.register_arg_completer(name, fn)` | 为任意命令扩展参数补全，含内建命令 | §6.2 |
| `ctx.register_tool(name=None, description="", usage="", schema=None, safe=False, safe_actions=None)` | 注册模型可见工具，支持装饰器用法 | §7 |
| `ctx.call(event, payload, default=None)` | 主动触发变换链事件 | §5.5 |
| `ctx.collect(event, payload)` | 主动触发观察链事件 | §5.5 |
| `ctx.register_ui_tree(fn, priority=100)` | 每帧提供主窗口会话树根节点，返回 `TreeNode` 或列表 | §4.6 |
| `ctx.register_ui_bottom(fn, priority=100)` | 每帧提供统计栏下方的 ANSI 文本行 | §4.6 |
| `ctx.storage_dir()` | 插件专属持久化目录，自动创建 | §4.3 |
| `ctx.register_teardown(fn)` | 注册卸载回调：卸载/重载时停线程、释放资源 | §12 |
| `ctx.runtime` | 高级兼容视图：运行时载体 `{app, runner}` 的对象引用 | §12 |
| `ctx.host` | 显式宿主服务，涵盖会话、文件、工具、命令、模型、插件管理等 | §15 |
| `ctx.session` | 当前会话对象，未就绪为 None | §15 |
| `ctx.get_setting(key, default=None)` / `ctx.set_setting(key, value)` | 本插件配置读取、校验和持久化 | §15 |
| `ctx.unregister_hook(event, handler=None, name="")` | 仅注销本插件匹配的订阅 | §15 |
| `ctx.interrupt_turn()` | 请求调度器中断回合，返回 bool | §15 |
| `ctx.execution_scope()` | 当前线程执行域：main/subagent/workflow/unknown，用于替代线程名前缀判身份 | §12 |
| `ctx.spawn_worker(fn, name="worker", daemon=True, stop_event=None)` | 启动插件后台工作线程，卸载自动兜底停止 | §12 |
| `ctx.current_worker` | worker 线程内返回自身句柄，句柄含 stop_event；非 worker 线程返回 None | §12 |
| `ctx.submit_turn(text, images=None)` | 以用户语义提交一条消息开启回合 | §4.4 |
| `ctx.run_when_idle(callback)` | 在调度器空闲锁内执行会话结构操作 | §15.4 |
| `ctx.notify(text)` | 用户可见通知，会写入会话系统消息 | §4.4 |
| `ctx.request_llm_retry()` | 请求重试当前失败的 LLM 请求，仅在 API 错误等待态有效 | §4.5 |

### 4.6 主窗口扩展：树节点与底部行

插件可把实时状态显示在主窗口中。两个回调都在 UI 帧线程调用，应保持快速、无阻塞；回调异常由 hook 系统隔离。

```python
from bawcode_sdk import PluginContext


def setup(ctx: PluginContext):
    TreeNode = ctx.ui.tree_node
    def tree_nodes(payload):
        return TreeNode(
            f"plugin:{ctx.plugin_id}:status",
            "插件 · 正在工作",
            "text",
            children=[TreeNode(f"plugin:{ctx.plugin_id}:detail", "当前任务：同步")],
        )

    def bottom_rows(payload):
        return ["\033[32m插件状态：运行中\033[0m", "插件提示：按 /sync 同步"]

    ctx.register_ui_tree(tree_nodes)
    ctx.register_ui_bottom(bottom_rows)
```

`register_ui_tree(fn, priority=100)` 的 `fn(payload)` 每帧返回 `TreeNode` 或节点列表；节点加入会话树根部，使用节点 ID 作为稳定唯一标识，可展开/折叠并显示 children。应以 `plugin:<插件 id>:` 为 ID 前缀避免冲突。

`register_ui_bottom(fn, priority=100)` 的回调每帧返回字符串或字符串列表，内容可含 ANSI 转义序列，绘制在输入框、项目/模型信息及会话状态统计栏之后；每个返回项占一行，宿主按终端宽度裁剪并将多行高度从会话区扣除。避免执行网络/磁盘操作或长时间计算。

### 4.3 持久化：`ctx.storage_dir(scope="project")`

scope 二选一，按数据的归属选：

- `scope="project"` 是缺省值：`{工作区}/.bawcode/plugin-data/<id>/`——按项目隔离，
  适合项目相关的索引、缓存和会话映射；插件升级覆盖目录时不会清除该目录
- `scope="global"`：`{宿主根}/data/plugin-data/<id>/`——跨项目共享，适合与项目无关且确实需要
  跨项目共享的数据，例如用户级收藏或通用模板。**注意全局目录无跨实例写锁**：多个 BAWCode
  实例并发跑时，插件须自行保证写入原子性，可以先写临时文件再 replace

不存在会自动创建。插件的状态文件、缓存、落痕都应写在这里。

```python
def setup(ctx):
    state_file = ctx.storage_dir() / "state.json"

    def _on_start(payload):
        state_file.write_text(json.dumps(payload), encoding="utf-8")

    ctx.register_hook("session_start", _on_start)
```

### 4.4 类用户操作接口：`submit_turn` / `notify` / `ctx.ui`

这组 API 让插件能"像用户一样"与主程序交互——发消息、收通知、弹面板。它依赖运行时注入，
main 创建回合调度器后会调用 `plugins.bind_runtime(app, runner)`。**启动极早期调用会
得到失败提示或 None，而不是异常**；hook 与命令的实际触发时机都在注入之后，正常使用无感。

#### `ctx.submit_turn(text) -> str` —— 以用户语义发一条消息

等价于用户在主输入框键入文字回车：

- 空闲时直接开新回合，模型收到这条消息并走完整工作流
- 忙碌时排队为当前回合结束后的新回合，插件外部消息不并入当前回合
- 返回状态说明字符串，例如 `[已提交，新回合开始]`；忙碌时返回
  `[已排队 · 当前回合结束后作为新回合发送]`，不带排队数量；空消息拒绝提交

```python
def setup(ctx):
    def _on_after_tool(payload):
        # 例：工具连续失败 3 次后代用户追问一次
        if payload.get("name") == "execute_command" and "错误" in (payload.get("output") or ""):
            ctx.submit_turn("刚才的命令失败了，请分析原因并给出修正方案")

    ctx.register_hook("after_tool", _on_after_tool)
```

> **防自激**：`submit_turn` 会开启新回合 → 又触发 hook → 可能再次 submit。凡是可能
> 循环触发自己的 handler，请加冷却，例如在 `ctx.storage_dir()` 里记时间戳，或只响应
> 明确的一次性条件。

#### `ctx.notify(text)` —— 用户可见通知

写入会话系统消息，在对话树中可见，呈现与命令反馈一致；同时刷新 UI 消息缓存，不直接渲染，
由帧循环统一上屏。适合"后台发生了你需要知道的事"这类轻量提醒；需要用户**回应**时用
`ctx.ui` 面板或 `submit_turn`。

#### `ctx.ui.confirm / choose / line / ask` —— 交互面板桥

这是 `app.request_ui/wait_ui` 的插件包装，与工具确认面板使用同一通道：

| 方法 | 面板 | 返回 |
|------|------|------|
| `ctx.ui.confirm(prompt, cancelled=None)` | 是/否选择 | `True` / `False`；取消 `None` |
| `ctx.ui.choose(options, prompt, cancelled=None)` | 序号选择菜单 | 所选项文本，支持序号或文本命中；自由文本原样返回；取消 `None` |
| `ctx.ui.line(prompt, default="", cancelled=None)` | 单行输入 | 输入文本，空输入回落 `default`；取消 `None` |
| `ctx.ui.ask(question, options=(), timeout=0, multiline=False, cancelled=None)` | 任务卡片，支持自由作答或选项作答，可多行、可超时 | `{status, answer?}`，其中 status ∈ answer/timeout/rejected；取消或通道未就绪时返回 `None` |

#### `ctx.ui.available` —— 调用面板前检查通道

这是布尔属性，运行时 `app` 已注入时为 `True`，否则为 `False`。`False` 时四个面板方法直接返回
`None`；需要交互才能继续的工具应提前判断并降级，`ask-user-subagent` 就是这样做的：

```python
if not ctx.ui.available:
    return "当前环境无交互通道，请根据现有信息继续处理。"
result = ctx.ui.ask("请补充部署目标", multiline=True)
```

它只表示通道已注入，不保证用户一定作答；取消、拒绝和超时仍按各方法的返回约定处理。

四个原语的 `cancelled` 参数省略时**自动接当前线程回合取消令牌**，即 `ctx.cancelled`。
工具执行内调用无需手工传探针；后台独立线程没有令牌，按不可取消处理。

```python
def _on_before_turn(payload):
    text = payload.get("user_text") or ""
    if text.startswith("/deploy"):
        env = ctx.ui.choose(["staging", "prod"], prompt="部署目标")   # agent 线程内可弹面板
        if env is None:
            # 面板取消没有"中止回合"通道：把输入改写成无操作说明，让模型简短收尾
            return {"user_text": "（部署命令已取消，本轮无需执行任何操作）"}
        return {"user_text": f"部署到 {env}：{text[len('/deploy'):].strip()}"}
    return None

ctx.register_hook("before_turn", _on_before_turn)
```

**线程约束，务必注意**：

- `ctx.ui` **只能在 agent/后台线程调用**，包括 hook、模型工具执行内和插件自起线程。面板的应答
  由主线程帧循环完成——主线程调用会被宿主防护**直接抛 RuntimeError**，不再是静默死锁；
  主线程请直接用 app 的阻塞表单，例如 `cctx["app"].read_line(...)` 等，参考 `/run` `/resume` 的做法
- 注意 `before_turn` 返回空白的语义是"不参与改写"，原输入照常进行，**没有中止回合的
  通道**——需要放弃任务时请改写成无操作说明，如上例

#### `ctx.request_llm_retry()` —— 请求重试失败的 LLM 请求

TUI 回合中，LLM 请求返回错误后可进入**错误等待态**：429 限频、5xx 或网络超时等瞬态错误在
自动重试耗尽后可手动重试，401、400、404 等确定性失败也进入同一等待态，可手动重试。
自动重试与手动重试是两套判断。终端状态行提示 `Ctrl+Y 重试 / Esc 放弃`，回合在此暂停，
不写任何会话消息。此时插件可调用本接口代替用户按键触发重试：

| 项 | 说明 |
|------|------|
| 返回 | `True` = 已触发重试；`False` = 当前无错误等待态，或用户已放弃 |
| 线程 | 任意线程，内部为事件置位，非阻塞 |
| 典型场景 | 远程控制端点的"重试"按钮、监控插件在检测到限频解除后自动放行 |

```python
def _api_retry(payload):
    if ctx.request_llm_retry():
        ctx.notify("[助手] 已代为触发重试")
    return None

ctx.register_external_api("llm_retry", _api_retry)
```

等待态的退出途径：Ctrl+Y 或本接口触发重试，同一请求重发，不计轮次、不影响会话；
Esc 放弃；提交新消息自动放弃，消息排队为当前回合结束后的新回合；
`config.llm.retry_wait_seconds` 超时，0 表示无限等待，也是默认值。


---

## 5. Hook 事件目录

### 5.1 两种调用语义

BAWCode 的每个 hook 事件属于两种语义之一，下表已标明，写处理函数前先分清：

**变换链 `call`** —— "可以改写输入"。处理函数按 `priority` 升序依次执行，
同优先级按注册顺序，规则如下：

- 返回 **`None`**：表示"不参与"，不影响链条，观察、记日志的场景都用它
- 返回 **`dict`**：与当前 payload **浅合并**后继续向后传递，后续处理函数拿到的是
  合并后的 payload；链条以 dict 结果结束时，最终返回**各 dict 贡献的浅合并**，后者覆盖前者
- 返回 **非 dict 非 None** 的值：暂记为最近结果，不终止链，也不把该返回值并入 payload；后续处理函数
  返回 dict 时，最终返回累计的 dict 浅合并结果，之前的非 dict 结果被遗忘；只有最后一个非 None
  结果为非 dict 时才原样返回它，并取代此前的 dict 贡献
- 全部返回 None，或没有处理函数：调用方使用 `default`，即回落本地默认逻辑

**`tool_confirm` 是特例**，不走统一合并链。处理函数仍按 priority 执行，首个返回合法
`allow_once`、`allow_always` 或 `deny` 的处理函数立即生效，后续处理函数不再执行，同时广播
`tool_confirm_closed` 让其他通道关闭同一请求；None 或非法返回值继续尝试下一个，全部无有效
应答才交回 TUI 确认面板。

**观察链 `collect`** —— "多方各自贡献"。入口只对原始 payload 浅拷贝一次，随后把同一个
data dict 依次传给所有处理函数，原地修改对后续处理函数可见；返回值之间不合并，所有非 None
返回值按执行顺序收集成列表交给调用方。处理函数抛异常只记日志，不影响其他处理函数。

### 5.2 事件总表

事件名同时以常量导出在 `bawcode_sdk.events`，例如 `AFTER_TURN = "after_turn"`，建议引用常量
获得 IDE 补全与重构可查性；`register_hook`/`register_external_api` 对目录外事件名会 warn 一次，
并附带相近拼写建议——插件间自定义互通事件属预期用法，可忽略该 warn。

#### 用户参与型能力：对应 `config.external_apis` 同名映射，采用变换链

| 事件 | 触发时机 | payload | 生效返回值 |
|------|----------|---------|-----------|
| `prompt_refine` | 任务提示完善阶段 | `{prompt: str}` | `{prompt: str}` 或 `str` |
| `plan_generate` | 计划生成，属 llm 外部接口；`write_plan`/`update_plan` 工具也会携带 `{plan, mode}` 触发 | `{prompt}` 或 `{plan, mode}` | `{title, content}` |
| `plan_confirm` | 计划确认回调 | `{plan, action, feedback}` | 仅返回 dict 且含真值 `plan` 键时接管，工作流收到 `external["plan"]`；其他返回值回落本地 action 处理 |
| `step_generate` | 步骤生成 | `{steps: [str]}` | `{steps: [str]}` —— 替换待写入的步骤 |
| `step_update` | 步骤状态变更通知 | `{step: {...}}` | 无消费方，仅作通知 |
| `memory_write` | 长期记忆落盘前 | `{memory, path, agent_md_path, project_md_path}` | 无消费方，可旁路同步到远端 |
| `memory_read` | 长期记忆读取 | `{path, agent_md_path, project_md_path}` | `{memory: {...}}` —— 整体替换读取结果 |
| `rag_add` | 保留的扩展点，当前宿主和内置 RAG 插件不自动触发 | 由主动调用方约定 | 无内置消费方，需自行 `ctx.call()` 触发 |
| `rag_query` | 保留的扩展点，当前宿主和内置 RAG 插件不自动触发 | 由主动调用方约定 | 无内置消费方，需自行 `ctx.call()` 触发 |
| `computer_use` | `computer_use` 工具调用 | `{action, params}` | 结果对象，可为 dict 或 str；None 则工具返回"未配置外部接口" |
| `llm_request` | 每次 LLM 请求前 | `{messages, tools, payload}` | chat 响应结构，如 `{content, tool_calls, ...}`—— **整段接管**该次请求；None 走正常请求 |
| `tool_confirm` | 工具确认面板弹出前 | `{name, arguments, request_id}` | 首个合法 `{action: "allow_once"}` / `{action: "allow_always"}` / `{action: "deny", reason: str}` 立即代答；None 继续后续处理函数，全部不参与才交回 TUI |
| `ui_request` | `confirm`、`choose`、`line`、`ask` 面板弹出前 | `{kind, ...面板参数}` | 返回与对应面板键盘输入同语义的非 None 应答即代答；None 交回终端面板 |

#### 生命周期事件

| 事件 | 语义 | 触发时机 | payload | 生效返回值 |
|------|------|----------|---------|-----------|
| `session_start` | collect | `init_session()` 初始化全局会话后；`/new` 和 `/resume` 不触发 | `{project_id, session_id, workspace}` | 无消费方 |
| `before_turn` | call | 回合开始、用户消息入档前 | `{user_text: str}` | `{user_text: str}` —— 改写本轮用户输入，返回空或空白视为不参与 |
| `after_turn` | collect | 回合收尾，正常、中断、异常都触发 | `{user_text, response, session_id}` | 无消费方 |
| `stream_delta` | collect | 流式输出增量，每 token 触发，频率高 | `{kind, piece}` | 无消费方 |
| `turn_status` | collect | 回合状态或阶段变化 | `{status}` 或 `{phase}` | 无消费方 |
| `before_tool` | call | 工具确认通过、执行前 | `{name, args}` | `{args: {...}}` 改写参数；`{decision: "deny", message: str}` 拒绝执行，拒绝文案回传给模型；None 原样执行 |
| `after_tool` | collect | 工具执行完成，含出错 | `{name, args, output, elapsed}` | 无消费方 |
| `tool_confirm_closed` | collect | 工具确认被其他通道应答、取消，或终端确认流程结束 | `{name, arguments, description, request_id, phase: "closed", action?}` | 无消费方；按 request_id 关闭其他通道的同一请求 |
| `user_message_supplement` | collect | 普通用户消息写入会话时，每条一次；插话、通知和隔离会话不触发 | `{project_id, session_id, user_message}` | `str`，须为非空文本——焊入该条 user 消息的 user_supplement 字段，出站以 `<system>` 块拼在正文开头；字节写入后冻结，随历史出站 |
| `message_added` | collect | 活动会话追加一条新消息后，隔离的工作流或节点会话不广播 | `{role, content, type?, ...extra}` | 无消费方，仅供观察、同步或转发 |
| `ui_tree_nodes` | collect | 每帧主窗口渲染 | `{app}` | `TreeNode` 或节点列表，加入会话树根部 |
| `ui_bottom_rows` | collect | 每帧主窗口渲染 | `{app, width}` | ANSI 字符串或字符串列表，显示在状态栏下方 |

`message_added` 的 `content` 可能是多模态数组。处理函数内禁止再次写会话消息，例如调用
`session.add_message()` 或 `ctx.notify()`，否则会递归触发本事件；只做观察和转发，耗时工作另派 worker。

### 5.3 用户消息补充的预算与礼仪

`user_message_supplement` 的文本写入后随消息**长期存在于历史**，字节会冻结，对前缀缓存友好，请克制：

- 单条建议 ≤ 200 字符；只注入"与该条用户消息语义绑定"的信息，例如按消息内容的检索召回、内容判定提示
- 不适合全局动态状态，如时间、计数器、任务进度——焊死后不再更新，会成为陈旧信息
- 可在处理函数里读取 `ctx.settings`，让用户按需开关，只影响后续写入的消息
- 文本以 `<system>` 块拼在 user 正文开头，不要自带大标题

### 5.4 实现 external_apis 扩展点

`external_apis` 是宿主为"核心能力可外置"预留的扩展点集合，通过 HTTP URL 挂接，见 §13。
插件可以用进程内函数直接实现同一批事件，免去部署独立 HTTP 服务：

```python
def setup(ctx):
    # 此后 computer_use 工具经该函数执行（优先级 50，与 external_apis HTTP 同层）
    def _computer_use(payload):
        action = payload.get("action")
        if action == "screenshot":
            return {"ok": True, "image": "...base64..."}
        return {"ok": False, "message": f"unsupported action: {action}"}

    ctx.register_external_api("computer_use", fn=_computer_use)
```

同事件混挂时按优先级排序：HTTP 外部接口 50、插件 `register_external_api` 缺省 50、
普通 `register_hook` 缺省 100。普通变换链中每个返回 dict 的处理函数都合并进最终结果，
因此"想接管的 handler 返回结果，想跳过的返回 None"即可共存；`tool_confirm` 按 §5.1 的
首个有效应答规则处理。

### 5.5 插件间互通

`ctx.call(event, payload, default)` / `ctx.collect(event, payload)` 让插件触发自定义
事件。事件名建议加 `<id>.` 前缀避免撞名，例如 `"my-plugin.translate"`。其他插件或宿主
代码可 `register_hook` 监听，形成松耦合管线。

---

## 6. 斜杠命令与参数补全

### 6.1 注册命令

```python
def setup(ctx):
    def _deploy(cctx, args):
        # cctx 与主程序命令同一上下文：{"llm", "session", "config", "app"}
        target = (args or "").strip() or "staging"
        return f"deploy -> {target}"   # 返回值会作为系统消息显示给用户
        # 返回 "EXIT" 可退出程序（别这么做）

    ctx.register_command(
        "/deploy",
        hint="部署到目标环境",
        usage="/deploy <staging|prod>",
        aliases=["/dp"],
        handler=_deploy,
    )
```

- 命令名不区分大小写，需以 `/` 开头，缺省会自动补
- **避免与内建命令同名**。首次启动时插件先注册，后注册的内建命令会反向覆盖插件同名命令；
  `/plugin reload` 路径下插件才会覆盖已存在的内建命令，并在插件卸载后恢复留底的内建版本
- 元数据 hint、usage、aliases 参与 `/help`、Tab 补全；`source` 固定为 `plugin:<id>`

### 6.2 参数补全

`completer` 参数或 `ctx.register_arg_completer` 可为命令注册参数补全器：

```python
def _complete_env(config, arg):
    items = [{"name": f"/deploy {e}", "hint": h, "source": "arg", "callable": True}
             for e, h in [("staging", "预发布"), ("prod", "生产")]]
    return [i for i in items if i["name"].lower().endswith((arg or "").lower())] or items

ctx.register_command("/deploy", hint="部署", handler=_deploy, completer=_complete_env)
```

`fn(config, arg) -> [{name, hint}, ...]`；`register_arg_completer` 对同一命令是**覆盖**而非追加。
给 `/model` 等内建命令注册会替换原补全器；插件卸载时只删除自己注册的补全器，原内建补全器
不会自动恢复。

---

## 7. 模型可见工具

`ctx.register_tool` 把函数注册进工具注册表——出现在每回合的工具定义，即 function calling
里，由模型按需调用，走**统一权限/确认/上下文限额管线**，与 `read`/`write` 同待遇。

```python
@ctx.register_tool(
    name="translate_text",
    description="Translate text between languages. Use when the user asks for translation.",
    usage="translate_text <text> [target_lang]",
    schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to translate"},
            "target_lang": {"type": "string", "description": "Target language, e.g. en, zh"},
        },
        "required": ["text"],
    },
)
def translate_text(text: str, target_lang: str = "en") -> str:
    return do_translate(text, target_lang)   # 返回 str（或可 JSON 序列化的对象）
```

要点：

- **`name` 必须全局唯一**：建议采用 `plugin 简名_功能` 的形式，例如 `mytr_translate`。与既有工具
  同名会覆盖内建工具，插件卸载后内建恢复
- **`description` 面向模型**：写清楚"什么时候该用"，否则模型不调用或乱调用；同时注明
  限制场景可避免滥用
- `schema` 是标准 JSON Schema，使用 `properties`/`required`；宿主会自动注入公共
  `description` 参数，模型用它留档调用意图，执行前剥除，不进入你的函数
- 函数按 `schema` 属性名以关键字参数调用，**形参名要与属性名一致**并给缺省值
- 权限：未列入 `policy.SAFE_TOOLS` 的新工具在 manual/confirm 模式下弹确认面板，用户"始终允许"
  后记入指纹白名单；auto 模式返回 `LLM_APPROVE`，由 `llm.approve_tool_call()` 裁决，批准后直接执行，
  不弹确认面板
- `safe=True` 免确认声明：纯交互、无副作用类工具，例如问用户、委派任务，在所有访问模式
  直接放行，与 `SAFE_TOOLS` 成员同待遇——先确认"能调用"纯属打扰。注意通用工具的
  "始终允许"指纹混入参数值、对变参调用永不复配，声明型 safe 是变参交互工具的唯一
  免确认通道；仅接受显式布尔 `True`，标志随插件卸载自动消失
- `safe_actions=[...]` 是 safe 的 action 级变体：按 `action` 参数分发内部操作的单工具，
  例如 computer_use 的 screenshot/window_list 观测动作，名单内 action 免确认、其余照常
  过门禁。只许声明纯观测动作——computer_use 可声明 `screenshot`，但 `click`/`type`
  这类输入动作绝不可入列
- 返回值超长部分按 `context.inline_limit_tokens` 外置落盘——不用担心大结果撑爆上下文

---

## 8. 插件自带技能

插件目录下的 `skills/<技能名>/SKILL.md` 自动并入技能系统，采用渐进式披露：启动只进元数据
清单，模型经 `load_skill` 按需读正文。

```
my-plugin/
└── skills/
    └── my-workflow/
        ├── SKILL.md        # YAML frontmatter + 正文
        ├── scripts/        # 可选
        └── references/     # 可选
```

```markdown
---
name: my-workflow
description: 一句话说明该技能解决什么问题、何时触发（供模型匹配任务）
---

# 我的工作流

1. 第一步……
2. 第二步……
```

优先级：**项目技能 `.bawcode/skills` > 插件技能 > 全局技能 `data/skills`**——同名
`name` 时后扫的覆盖先扫的。格式规范与 `data/skills` 完全一致，包括 frontmatter 字段、
scripts/references 约定。

---

## 9. 配置参考

### 9.1 配置声明与设置面板"插件"标签页

在 `plugin.json` 中声明 `config` 数组，插件即拥有**可视化配置界面**：设置面板
`/settings` 新增"插件"标签页，列出识别到的全部插件，含禁用和失败插件，并附原因：

- **插件行**：`←→` 开/关插件，会写盘 `config.plugins.disable` 并立即重载生效；
  `Enter` 展开/收起该插件的配置列表，列表采用树状缩进，仅声明了 config 的插件可展开
- **配置行**：按声明类型渲染不同控件——`bool` 勾选框用 `[x]`/`[ ]` 表示，Enter/←→ 切换；
  `list` 左右键循环切换选项，复用设置页既有交互；`str`/`int`/`float` Enter 后行编辑，
  int/float 带类型校验与 min/max 钳制，非法输入保留原值
- 所有改动**即时写透**到 `config.plugins_config[<id>]` 并落盘，插件经
  `ctx.settings` 实时读取，无需重载

声明格式如下，支持 `str`/`list`/`int`/`float`/`bool` 五种类型：

```jsonc
{
  "id": "my-plugin",
  // ...
  "config": [
    { "key": "endpoint",  "type": "str",   "default": "",              "label": "接口地址" },
    { "key": "mode",      "type": "list",  "options": ["fast", "safe"], "default": "fast", "label": "运行模式" },
    { "key": "retries",   "type": "int",   "default": 3, "min": 0, "max": 10, "label": "重试次数" },
    { "key": "ratio",     "type": "float", "default": 0.5, "label": "采样比率" },
    { "key": "verbose",   "type": "bool",  "default": false, "label": "详细日志", "hint": "调试用" }
  ]
}
```

| 字段 | 适用类型 | 说明 |
|------|----------|------|
| `key` | 全部 | 配置键名，须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，可以下划线开头，只能含字母、数字和下划线，即 `ctx.settings` 与 `plugins_config` 里的键 |
| `type` | 全部 | `str` / `list` / `int` / `float` / `bool`；非法声明的项被丢弃并记录 warn 日志，不影响插件装载 |
| `label` | 全部 | 设置面板显示名，缺省用 key |
| `hint` | 全部 | 设置面板底部的说明文字 |
| `default` | 全部 | 缺省值，按类型规整；`list` 的 default 必须命中 options，否则取首项 |
| `options` | list | **必填**，非空字符串数组，左右键在其间循环 |
| `min` / `max` | int/float | 可选数值范围，写入时钳制 |

程序侧 API 在 UI 之外同样可用：`plugins.declared_configs(pid)` 取声明、
`plugins.get_setting(pid, key, default)` 读、`plugins.set_setting(pid, key, value, config)`
写，按声明规整钳制后写盘。

### 9.2 配置文件段

`data/config.json` 中与插件相关的段均有内置缺省，可只写覆盖项：

```jsonc
{
  // 插件系统开关与位置
  "plugins": {
    "enabled": true,          // false = 全局关闭，所有插件不装载
    "dir": "data/plugins",    // 全局插件目录（相对路径相对 BAWCode 根）
    "disable": ["some-id"],   // 禁用的插件 id 列表（/plugin enable/disable 维护）
    "auto_install_deps": true // 装载时按插件 requirements.txt 静默补装缺失依赖（见 §2.3）
  },

  // 插件私有配置：键 = 插件 id；声明过的键由设置面板"插件"页维护，
  // 未声明的键插件可自行约定（经 ctx.settings 原样读取）
  "plugins_config": {
    "my-plugin": {
      "endpoint": "https://example.com/api",
      "verbose": true
    }
  },

  // 核心能力外置 HTTP 接口（插件可用 register_external_api 进程内实现，见 §5.4）
  "external_apis": {
    "computer_use": null,
    "prompt_refine": null
    // ... 其余见 core/config.py default_config()
  }
}
```

配置只有两层合并，读取优先级从低到高为：**声明缺省 < `plugins_config` 持久化值**。
动态行为请在处理函数内读取 `ctx.settings`，每次访问都会实时重读，不要在 `setup()` 时缓存。

设计约定：`plugins_config` 只放**行为开关与参数**，不放 api_key 等秘密——插件进程内
运行，读得到整个 config，请勿在文档中诱导用户把敏感信息交给不可信插件。

---

## 10. /plugin 命令

| 命令 | 作用 |
|------|------|
| `/plugin` | 列出全部插件：id、版本、状态、来源、注册量。状态包括装载、安装中、禁用、失败，注册量涵盖 hooks、tools、commands；失败插件附错误原因；末尾附当前 hook 扩展点占用概览 |
| `/plugin reload` | 重新发现并装载全部插件，先整体卸载。开发插件时改完代码执行它，无需重启 |
| `/plugin disable <id>` | 禁用插件并**写回** `config.plugins.disable` 以持久化，随后自动重载 |
| `/plugin enable <id>` | 从 disable 列表移除并重载；若清单 `enabled: false` 仍不会装载 |

状态含义：

- `[装载]` —— setup 执行成功，注册项生效
- `[安装中]` —— TUI 正在后台安装缺失依赖，尚未导入入口或执行 setup，完成后自动补装载
- `[禁用]` —— 因 §3 的启用优先级被跳过，error 字段说明原因
- `[失败]` —— 装载抛异常，包括导入错误和 setup 内错误，**已注册的部分会整体回滚**，
  不影响其他插件与主流程

---

## 11. 调试

1. **看状态**：`/plugin` —— 注册量、失败原因一目了然
2. **看日志**：`data/log/` 下按天滚动；插件日志在 `plugins.<id>` 命名空间。把
   `config.log.modules` 中 `"plugins": "debug"` 打开，或设置为 `{"plugins.<id>": "debug"}`，
   可看详细过程：

   ```json
   "log": { "level": "info", "modules": { "plugins": "debug", "hooks": "debug" } }
   ```

3. **hook 链诊断**：`ctx.host.hooks.list_hooks()`返回
   `{事件: [{owner, priority, name}]}`，核对注册与优先级
4. **常见错误对照**：

   | 现象 | 原因 |
   |------|------|
   | `/plugin` 看不到插件 | 目录在 `_`/`.` 开头目录里，发现阶段会跳过 |
   | 状态 `[失败]`，提示插件 id 非法 | 按 §3 修正 id；非法 id 仍以失败状态列出，不会从列表消失 |
   | 全部插件状态 `[禁用]` | 检查 `config.plugins.enabled`；全局关闭时插件仍列出并显示禁用原因 |
   | 状态 `[失败]`，报 `SyntaxError` | main.py 语法错误，先 `python -m py_compile main.py` |
   | 状态 `[失败]`，报 `FileNotFoundError: 入口文件不存在` | entry 文件名拼错 |
   | 命令补全可见但执行报 no_handler | 只注册了元数据没给 handler |
   | 模型不调用你的工具 | description 不够明确；schema 与函数形参不一致，可看日志"工具 X 参数错误" |
   | hook 没触发 | 事件名拼错，可对照 §5.2 总表；变换链事件里返回了 None |
   | 改了代码 /plugin reload 不生效 | 确认装载的是这一份，注意 global 与 project 的覆盖关系；`__pycache__` 陈旧时可删除插件目录下缓存 |

---

## 12. 生命周期与线程模型

```
启动 main()
 ├─ Config 加载
 ├─ plugins.load(async_deps=True)       ← TUI 插件发现与首轮装载
 │   ├─ discover()                     双层目录扫描 + 覆盖解析
 │   ├─ _load_one() × N                依赖就绪：import 入口 → setup(ctx) → 登记注册量
 │   │                                 缺依赖：[安装中] + 后台 pip，暂不 import/setup
 │   │                                 单插件失败：回滚其注册，继续下一个
 │   └─ _sync_plugin_skills()           已装载插件的 skills/ 并入技能系统
 ├─ init_session()                     ← session_start 事件触发
 ├─ runner 创建 + bind_runtime(app, runner)   ← 类用户操作 API 载体注入（§4.4）
 └─ 主循环 / agent 线程                 ← before_turn / before_tool / after_tool / after_turn ...
     └─ plugins.tick()                  依赖安装结束后在主线程补装载
```

无头模式使用 `async_deps=False`，依赖安装和补装载都在 `init_session()` 前同步完成。TUI 后台补装
发生在首次 `session_start` 之后，这类插件收不到启动时的那一次事件。

- **装载在主线程**，但 hook 调用多发生在 **agent 后台线程**；hook 注册表有锁保护，
  注册表锁并不保证插件业务线程与重载原子：在途调用可能拿到旧回调。请在空闲时重载，并在 teardown 中停止、唤醒和等待自己的后台任务
- **类用户操作 API 的可用时机**：`bind_runtime` 注入后，也就是回合调度器创建之后，`ctx.ui`
  与 `ctx.submit_turn` 才生效；此前调用返回失败提示或 None，不抛异常。`/plugin reload`
  不影响已注入的运行时载体
- **重载语义**：整体卸载，按 owner 注销 hooks/commands/tools、清理 sys.modules 与
  sys.path、清空插件技能目录，然后重新发现装载。模块级全局变量不保留——需要跨重载的
  状态请存 `ctx.storage_dir()`
- **后台资源清理，必须执行**：setup 里启动线程、HTTP 服务器等长期资源的插件，务必
  `ctx.register_teardown(fn)` 注册卸载回调——卸载、重载、禁用回滚时**按后进先出顺序，即 LIFO，执行**。
  异常会隔离并记日志。没有它，`/plugin reload` 后旧线程会残留且插件自身无法停止，
  因为模块对象已换新。官方示例：remote-control 插件的 HTTP 服务即经 teardown 停止
- **后台工作线程用 `ctx.spawn_worker(fn, name="worker", daemon=True, stop_event=None)`**：返回 Worker 句柄，
  包括 `worker.thread`/`worker.stop_event`/`worker.stop(timeout)`。fn 是普通无参函数，
  停止信号两种用法可混用——闭包自管，也就是传自己的 stop_event、循环里查 `is_set()`；或
  线程内自省，可在任意深度用 `ctx.current_worker.stop_event`。close 时自动置位全部
  worker 的停止信号并限时 join，每线程限时 2 秒：**即使忘记注册 teardown 也不会永久残留**，
  未退出仅记 warn。worker 里 `ctx.cancelled` 为 None 是正确语义——后台 worker 的取消
  信号是自己的 stop_event 与插件卸载，不响应单次回合中断
- **执行域判定 `ctx.execution_scope()`**：返回 `main` 表示主代理回合，`subagent` 表示后台
  子代理任务，`workflow` 表示工作流实例，`unknown` 表示未标记线程，含插件 worker 与主线程。
  需要"禁止子代理递归调用本插件"这类判定时用它，不要按线程名前缀硬编码
- **运行时载体只读访问**：`ctx.runtime` 返回 `{app, runner}`，即 `bind_runtime`
  注入的同一对象引用；未注入时值为 None。可读取 `app.busy / app.streaming_msg /
  app.status` 等实时状态、`runner.busy` 判定忙闲，或 `runner.llm.cancel()` 中断在途
  请求，remote-control 的"停止"按钮即此实现。约定只读，勿替换其中对象
- **模块隔离**：SDK v1 插件不加入 `sys.path`，入口与相对导入的子模块放在插件独立命名空间。卸载会清理整个命名空间，重载直接编译源码，避免同秒修改命中旧字节码。省略 `sdk_version` 的旧插件保留绝对导入路径兼容，但其绝对导入辅助模块不保证隔离；请迁移到相对导入。
- hook 处理函数**不要长时间阻塞**：`before_tool`/`before_turn` 在关键路径上，阻塞会
  卡住整个回合；耗时工作请放线程并及时返回

---

## 13. 兼容性：旧扩展方式

插件系统落地后，以下旧机制全部保留并**统一到 hook 链**上：

| 旧方式 | 现状 |
|--------|------|
| `config.external_apis.<事件> = URL` | 不变。启动时挂接为 `owner=external_apis`、优先级 50 的 HTTP POST 处理函数；请求体 `{"event", "payload"}`，响应 JSON 作为扩展点返回值；失败回落本地逻辑。与插件 handler 同链共存，按优先级与"None=不参与"协作 |
| `hooks.register_hook(event, fn)` 进程内注册 | 不变，签名兼容，新增 `priority/owner/name` 关键字参数。单事件从"单槽覆盖"变为"多处理函数链"——旧行为等价于链上只有一个处理函数 |
| `data/commands/*.py` 命令插件 | 不变，仍使用 `commands.load_plugins`，配置项为 `command_plugins`。新插件建议迁移到本系统以获得 owner 卸载与 `/plugin` 管理 |
| `core/register.register` 工具装饰器 | 不变，新增 `owner` 参数。内建工具 owner 为空，不受插件卸载影响 |

---

## 14. FAQ

**Q：插件能 hook 其他插件吗？**
能。`ctx.call/collect` 触发自定义事件，`ctx.register_hook` 监听任意事件，含其他插件
的自定义事件。装载顺序按目录名排序，有依赖时在 setup 里防御性判断，因为对方可能未装载。

**Q：插件能发 LLM 请求吗？**
通常使用 `ctx.submit_turn()` 交给宿主调度，不要在后台线程并发调用当前主回合的 LLM。独立推理可通过第三方模型客户端和插件自己的配置实现；SDK v1 暂未提供独立 LLM 会话服务。整段接管主对话请用 `llm_request` 扩展点。

**Q：能改模型返回内容吗？**
`llm_request` 可整段接管；`before_turn` 可改写用户输入；`before_tool` 可改写工具参数或
拒绝工具。没有"事后改 assistant 输出"的扩展点——那是故意的，目的是避免插件篡改对话历史。

**Q：插件能像用户一样发消息、弹面板吗？**
能，见 §4.4：`ctx.submit_turn(text, images=None)` 等价用户发送，busy 时排队；
`ctx.notify(text)` 写用户可见通知；`ctx.ui.confirm/choose/line` 弹交互面板，仅 agent
线程可用。注意 submit_turn 的自激风险——循环触发自己的 hook 请加冷却。

**Q：插件冲突怎么排查？**
`/plugin` 看 owner 与注册量 → `hooks.list_hooks()` 看同一事件上有哪些 owner → 临时
`/plugin disable <id>` 二分定位。

**Q：想做图形界面/悬浮窗？**
插件可自起线程或子进程，例如使用tkinter，但注意 TUI 终端独占前台：GUI 进程应是独立窗口且
不抢终端焦点；退出前清理线程。

---

*本指南对应实现：`core/plugin_system/` 负责宿主管理与适配，`bawcode_sdk/` 提供公开接口，
`core/plugins.py` 是兼容入口，`core/hooks.py` 实现 hook 链；
示例插件：`data/plugins/hello-plugin/` 是官方完整入门示例；`data/plugins/emotion-detector/`
展示与消息语义绑定的情绪提示，两者都使用 register_user_message_supplement，可正常装载；
`data/plugins/rag/` 是项目 RAG 知识库，也是自核心剥离的真实案例，注册 rag_search、rag_index、
set_rag 三个工具和 user_message_supplement，不接管 rag_add/rag_query external_apis；
`data/plugins/remote-control/` 是远程控制综合案例，涵盖后台 HTTP 服务、register_teardown、
类用户操作 API 和 tool_confirm 代答；
端到端测试：`develop/test_plugins_e2e.py`、`develop/test_rag_plugin.py`、`develop/test_remote_control_e2e.py`。*


## 15. SDK v1：架构、宿主服务与迁移

### 15.1 稳定入口和依赖方向

```text
插件 -> bawcode_sdk.PluginContext -> HostAPI -> core/plugin_system/adapter.py
                                                -> 宿主命令、工具、会话等实现
```

`bawcode_sdk` 只依赖 Python 标准库，导入它不会导入 `core`、终端界面或模型客户端。
SDK 随 BAWCode 安装，无需插件另外安装；包含 `py.typed`，可直接用类型注解。
其他宿主或单元测试可以实现 `HostAPI.invoke(service, operation, *args, **kwargs)` 与
`HostAPI.value(service, name)`，再通过 `PluginContext(..., host=adapter)` 注入。
这不意味着 BAWCode 的 Config/Session/App 对象已成为跨宿主通用数据模型：它们仍是高级兼容对象，见下文。

宿主侧职责：
- `discovery.py`：双层目录、清单和 SDK 版本检查。
- `dependencies.py`：依赖检查与可选 pip 安装。
- `loader.py`：入口边界检查、包命名空间、源码重载和卸载清理。
- `manager.py`：生命周期、状态、技能同步、配置持久化。
- `runtime.py`：延迟绑定 app/runner、通知、中断。
- `adapter.py`：公开服务到内部实现的唯一映射点。
- `core.plugins`：保留主程序 `/plugin`、设置页和旧插件的公开管理函数及旧构造方式。
  私有 `_loaded`、`_runtime`、`_pip_install` 不属于兼容 API；测试应在对应实现模块打桩。

### 15.2 新增便捷接口

```python
from bawcode_sdk import PluginContext


def setup(ctx: PluginContext):
    @ctx.register_command('/sample-toggle')
    def toggle(command_ctx, args):
        enabled = ctx.set_setting('enabled', not ctx.get_setting('enabled', True))
        return f'enabled={enabled}'

    def observer(payload):
        ctx.log.info('turn finished')

    ctx.register_hook('after_turn', observer)
    # 临时订阅可调用 ctx.unregister_hook('after_turn', observer)；只影响本插件。
    # setup 也可返回一个无参数清理函数，等效于 register_teardown。
    return lambda: ctx.log.info('stopped')
```

`set_setting` 按声明转换、钳制并保存；无效 bool、NaN/Infinity 会被拒绝。
持久化失败会恢复原内存值并抛出异常，不会假报保存成功。
卸载清理按后进先出顺序，即 LIFO 执行，且每个 context 只执行一次；单个清理函数的 Exception/SystemExit
不阻断后续清理。关闭后的 context 不允许重新注册命令、工具或 hook。
`ctx.ui.tree_node(...)` 创建与终端兼容的树节点，无需导入 `core.ui.TreeNode`。

### 15.3 高级宿主服务

管理型插件经 `ctx.host.<服务>.<操作>` 使用显式白名单服务，不再直接导入 `core` 模块。
所有可调用操作和常量列在 `bawcode_sdk/host.py` 的 `SERVICE_METHODS` / `SERVICE_VALUES` 中；
不存在的服务操作抛出 `AttributeError`，不会任意转发宿主私有成员。

| 服务 | 主要能力 |
| --- | --- |
| `sessions` | 当前会话、消息纯文本/图片数、会话目录、列表、读取、衍生记录删除 |
| `files` | 文件补全候选、引用格式、读取外置工具输出 |
| `commands` | 命令列表、补全、解析和执行 |
| `tools` | 工具列表、元数据、失败提示 |
| `plugins` | 插件列表、配置声明与读写、启停、重载 |
| `models` | 模型显示名、模型 API 地址规范化 |
| `settings` | `fields(tab, config)` 列出标签页字段；`read_field(config, field)` 读取；`write_field(config, field, raw)` 按声明转换并写入配置对象 |
| `workflows` / `snapshots` | 工作流列表/选择、快照查询与回滚 |
| `agents` / `skills` / `mcp` | 子代理记录、技能加载器、MCP 状态 |
| `policy` | 模式常量与显示标签 |
| `hooks` | 订阅、触发、诊断；`register_hook`/`unregister_hook` 自动归属调用方插件，注册缺省带 owner，注销缺省只动本插件订阅，显式传 owner 才可跨插件操作 |

这些高级操作保留对应宿主函数的参数、返回结构与副作用；例如：

```python
session = ctx.session
if session is not None:
    directory = ctx.host.sessions.sessions_dir(ctx.config, session.project_id)
    rows = ctx.host.sessions.list_sessions(directory)
items = ctx.host.commands.complete('/hel', config=ctx.config)
```

注册扩展请优先使用 `ctx.register_*`，以自动附带 owner 和清理检查。
高级服务不是安全沙箱，也不会替调用者自动做远程身份认证、确认、空闲锁或权限审批。
尤其 `commands.execute`、会话删除、快照回滚与配置修改须由管理插件自行鉴权，并在适当时机
通过 `ctx.run_when_idle(callback)` 串行化。不要经底层工具注册表执行模型工具以绕过正常审批管线。
`ctx.config`、`ctx.session`、`ctx.runtime` 仍暴露活对象，便于现有管理插件完整迁移；
不承诺其私有字段稳定，也不承诺任意线程读写安全。通用插件优先使用高层方法。

### 15.4 空闲锁：`ctx.run_when_idle(callback)`

`ctx.run_when_idle(callback)` 在与消息提交仲裁共用的调度锁内检查回合状态并执行无参数 callback，
适合新建、清空、恢复会话等结构操作，避免与正在进行的回合或消息提交并发：

- 空闲时持锁执行 callback，并原样返回 callback 的结果；锁可重入，callback 内可再调用调度器的原子方法
- 回合忙碌时不执行 callback，返回 `{"ok": False, "message": "回合进行中：请先停止后再操作"}`
- 调度器未注入或不支持原子会话操作时，返回 `{"ok": False, "message": "调度器不支持原子会话操作"}`

它只提供串行化，不代替远程鉴权或权限审批。callback 应保持短小，不要放入网络请求等耗时工作，
否则会阻塞消息提交仲裁

### 15.5 验证

```bash
python -m compileall -q bawcode_sdk core/plugin_system data/plugins
```

发布前可在宿主项目的临时工作区验证插件发现、SDK 版本拒绝、相对导入隔离、重载清理、
失败回滚与配置持久化；不要把测试夹具或临时测试脚本放入发布源码。

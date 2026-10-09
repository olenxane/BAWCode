# BAWCode 插件开发指南

BAWCode 插件系统把 **hook 扩展点、斜杠命令、模型可见工具、技能（Skills）、上下文补充、
类用户操作接口**（发消息/通知/交互面板）
打包为一个可分发的目录，放进插件目录即可生效，无需改动主程序代码。

- 插件 = 一个目录：`plugin.json`（清单）+ `main.py`（入口）+ 可选的 `skills/` 等资源
- 装载时机：BAWCode 启动时自动发现并装载（先于会话初始化，可收到 `session_start`）
- 官方完整示例：`data/plugins/hello-plugin/`（本指南所有 API 它都在用）

> **信任模型**：插件在本进程内以完整权限运行 Python 代码（可读写文件、发起网络请求）。
> 只安装可信来源的插件；分发前请阅读插件源码。

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

30 秒写一个能用的插件：

**第 1 步**：创建目录 `data/plugins/my-first-plugin/`，放入两个文件。

`plugin.json`：

```json
{
  "id": "my-first-plugin",
  "name": "My First Plugin",
  "version": "0.1.0",
  "description": "演示：一条命令 + 一个 hook"
}
```

`main.py`：

```python
def setup(ctx):
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

**第 2 步**：启动 BAWCode（或已在运行中执行 `/plugin reload`）。输入 `/ping-me` 验证；
`/plugin` 可看到插件状态为 `[装载]`。

就这样。下面展开每个细节。

---

## 2. 目录结构与发现规则

### 2.1 插件目录（双层，与技能同范式）

| 层级 | 位置 | 说明 |
|------|------|------|
| 全局 | `data/plugins/<插件目录>/` | 所有项目可用；可用 `config.plugins.dir` 改到别处 |
| 项目 | `{工作区}/.bawcode/plugins/<插件目录>/` | 仅当前项目可用；**同名插件覆盖全局版** |

覆盖规则：两个层级出现**相同 `id`** 的插件时，只装载项目级那份（日志会提示覆盖）。
临时/私有辅助目录以下划线或点开头（如 `_drafts/`）会被跳过。

### 2.2 插件目录内部结构

```
my-plugin/
├── plugin.json        # 清单（推荐但非必需，见 §3）
├── main.py            # 入口：定义 setup(ctx)（entry 可指定其他文件名）
├── requirements.txt   # 可选：插件独有依赖，装载时缺失项自动补齐（见 §2.3）
├── skills/            # 可选：插件自带技能，自动并入技能系统（见 §8）
│   └── my-usage/
│       └── SKILL.md
├── lib/               # 可选：插件自己的辅助模块（装载期间插件目录在 sys.path 上，可直接 import）
└── （其他任意资源文件）
```

最小要求：目录里有 `main.py` 即可装载（此时清单字段全部取缺省值，`id` 取目录名）。
**没有 `main.py` 且没有 `plugin.json` 的目录会装载失败**（状态 `[失败]`，不影响其他插件）。

### 2.3 插件依赖（requirements.txt）

插件的第三方依赖**不随主程序安装**——请在插件目录放一份 `requirements.txt`（标准 PEP 508
格式，每行一个依赖，支持版本约束与 `#` 注释），宿主会在**首次装载该插件时**检查并按需
静默 `pip install -r requirements.txt` 补齐缺失项：

```
# requirements.txt 示例
websockets>=13
Pillow>=10
```

规则与注意：

- **只检查、只安装缺失项**：依赖已满足时零开销跳过（不调用 pip）；版本不满足（如已装
  `websockets 12` 而要求 `>=13`）也视为缺失并补装
- 安装用**当前运行 BAWCode 的解释器**（`sys.executable -m pip`），装进同一环境；完成后
  复核，个别仍不满足的记入 `/plugin` 诊断
- **失败不阻断装载**：pip 失败（断网/无 pip）只写日志（`data/log`，命名空间 `plugins`），
  插件照常 `import + setup`——依赖可选的插件应自行 `try/except ImportError` 降级
  （如自带工具缺依赖时不注册、命令给出缺失提示）
- 关闭自动安装：`config.plugins.auto_install_deps = false`（此时仅检查，缺依赖状态显示在
  `/plugin` 的"未自动装"标注里，需用户自行安装）
- 插件依赖请在 `requirements.txt` 声明，**不要**写进主程序根目录的 `requirements.txt`
- 安装可能耗时（下载大型包），且发生在启动/装载阶段——首次装载会短暂阻塞，属预期

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
| `version` | string | `"0.0.0"` | 版本号，`/plugin` 列表展示 |
| `description` | string | `""` | 一句话描述，`/plugin` 列表展示 |
| `author` | string | `""` | 作者 |
| `entry` | string | `"main.py"` | 入口文件名（相对插件目录）。文件必须存在且能被 Python 导入 |
| `enabled` | bool | `true` | 清单级开关。`false` 时不装载（状态 `[禁用]`）。运行期启停用 `/plugin enable/disable`，优先级见下 |

**启用优先级**（从高到低，任一命中即不装载）：

1. `config.plugins.enabled = false` —— 插件系统全局关闭
2. 插件 `id` 出现在 `config.plugins.disable` 列表 —— 单插件禁用（`/plugin disable` 写入）
3. 清单 `enabled: false`

---

## 4. PluginContext API 参考

`setup(ctx)` 收到的 `ctx` 是 `core.plugins.PluginContext` 实例，这是插件的全部能力面。
所有经 ctx 注册的内容自动携带 `owner=插件 id`，插件卸载/重载时**整体注销**，无需手工清理。

### 4.1 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `ctx.plugin_id` | str | 插件 id |
| `ctx.dir` | Path | 插件目录绝对路径 |
| `ctx.manifest` | dict | 解析后的清单（含缺省补全） |
| `ctx.source` | str | `"global"` 或 `"project"` |
| `ctx.config` | Config | 宿主配置对象（**只读约定**：读模型名/主题等可以，改配置请走 `/settings`） |
| `ctx.workspace` | Path | 当前工作区根目录 |
| `ctx.settings` | dict | 插件配置**实时视图**（声明缺省 + `config.plugins_config[<id>]` 持久化值）。每次访问重读宿主配置——设置面板改动即时生效；动态行为请在处理函数内读取，勿在 setup 时缓存 |
| `ctx.log` | Logger | 命名空间化日志器（`plugins.<id>`），落盘到 `data/log/` |
| `ctx.ui` | PluginUI | 交互面板桥（confirm/choose/line），**仅 agent 线程可用**，见 §4.4 |

### 4.2 注册方法

| 方法 | 用途 | 详细 |
|------|------|------|
| `ctx.register_hook(event, fn=None, priority=100, name="")` | 注册 hook 处理函数（可作装饰器） | §5 |
| `ctx.register_context_supplement(fn, priority=100)` | 每回合注入上下文文本 | §5.3 |
| `ctx.register_external_api(event, fn=None, url="", priority=50)` | 实现 external_apis 同名扩展点 | §5.4 |
| `ctx.register_command(name, hint="", usage="", aliases=None, handler=None, completer=None)` | 注册斜杠命令 | §6 |
| `ctx.register_arg_completer(name, fn)` | 为任意命令（含内建）扩展参数补全 | §6.2 |
| `ctx.register_tool(name=None, description="", usage="", schema=None)` | 注册模型可见工具（装饰器） | §7 |
| `ctx.call(event, payload, default=None)` | 主动触发变换链事件 | §5.5 |
| `ctx.collect(event, payload)` | 主动触发观察链事件 | §5.5 |
| `ctx.storage_dir()` | 插件专属持久化目录（自动创建） | §4.3 |
| `ctx.register_teardown(fn)` | 注册卸载回调：卸载/重载时停线程、释放资源 | §12 |
| `core.plugins.runtime()` | 模块级函数：只读运行时载体 `{app, runner}` | §12 |
| `ctx.submit_turn(text)` | 以用户语义提交一条消息开启回合 | §4.4 |
| `ctx.notify(text)` | 用户可见通知（写入会话系统消息） | §4.4 |
| `ctx.request_llm_retry()` | 请求重试当前失败的 LLM 请求（仅 API 错误等待态有效） | §4.5 |

### 4.3 持久化：`ctx.storage_dir()`

返回 `{工作区}/.bawcode/plugin-data/<id>/`（不存在会自动创建）。插件的状态文件、缓存、
落痕都应写在这里——目录随工作区隔离，且不会被插件升级（覆盖目录）清除。

```python
def setup(ctx):
    state_file = ctx.storage_dir() / "state.json"

    def _on_start(payload):
        state_file.write_text(json.dumps(payload), encoding="utf-8")

    ctx.register_hook("session_start", _on_start)
```

### 4.4 类用户操作接口：`submit_turn` / `notify` / `ctx.ui`

这组 API 让插件能"像用户一样"与主程序交互——发消息、收通知、弹面板。依赖运行时注入
（main 创建回合调度器后调用 `plugins.bind_runtime(app, runner)`），**启动极早期调用会
得到失败提示/None 而不是异常**；hook 与命令的实际触发时机都在注入之后，正常使用无感。

#### `ctx.submit_turn(text) -> str` —— 以用户语义发一条消息

等价于用户在主输入框键入文字回车：

- 空闲时直接开新回合（模型收到这条消息，走完整工作流）
- 忙碌时按 `ui.busy_send_mode` 设置处理：`queue` 排队接力 / `interrupt` 中断在途回合
- 返回状态说明字符串（如 `[已提交，新回合开始]`、`[已排队 N 条 · 本轮结束后自动发送]`），
  空消息拒绝提交

```python
def setup(ctx):
    def _on_after_tool(payload):
        # 例：工具连续失败 3 次后代用户追问一次
        if payload.get("name") == "execute_command" and "错误" in (payload.get("output") or ""):
            ctx.submit_turn("刚才的命令失败了，请分析原因并给出修正方案")

    ctx.register_hook("after_tool", _on_after_tool)
```

> **防自激**：`submit_turn` 会开启新回合 → 又触发 hook → 可能再次 submit。凡是可能
> 循环触发自己的 handler，请加冷却（如 `ctx.storage_dir()` 里记时间戳）或只响应
> 明确的一次性条件。

#### `ctx.notify(text)` —— 用户可见通知

写入会话系统消息（对话树可见，呈现与命令反馈一致），并刷新 UI 消息缓存（不直接渲染，
由帧循环统一上屏）。适合"后台发生了你需要知道的事"这类轻量提醒；需要用户**回应**时用
`ctx.ui` 面板或 `submit_turn`。

#### `ctx.ui.confirm / choose / line` —— 交互面板桥

`app.request_ui/wait_ui`（工具确认面板同款通道）的插件包装：

| 方法 | 面板 | 返回 |
|------|------|------|
| `ctx.ui.confirm(prompt, cancelled=None)` | 是/否选择 | `True` / `False`；取消 `None` |
| `ctx.ui.choose(options, prompt, cancelled=None)` | 序号选择菜单 | 所选项文本（支持序号或文本命中）；自由文本原样返回；取消 `None` |
| `ctx.ui.line(prompt, default="", cancelled=None)` | 单行输入 | 输入文本（空输入回落 `default`）；取消 `None` |

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

**线程约束（重要）**：

- `ctx.ui` **只能在 agent 线程调用**（hook / 模型工具执行内）。面板的应答由主线程帧循环
  完成——在主线程命令 handler 里调用会把帧循环堵死造成死锁；主线程请直接用 app 的
  阻塞表单（`cctx["app"].read_line(...)` 等，`/run` `/resume` 的做法）
- `cancelled` 回调可选传入（如 `llm.cancelled`）；回合被用户中断时面板应尽快放弃
- 注意 `before_turn` 返回空白的语义是"不参与改写"（原输入照常进行），**没有中止回合的
  通道**——需要放弃任务时请改写成无操作说明（如上例）

#### `ctx.request_llm_retry()` —— 请求重试失败的 LLM 请求

回合遇到瞬态 API 错误（429 限频、5xx、网络超时）且自动重试耗尽后，进入**错误等待态**：
终端状态行提示 `Ctrl+Y 重试 / Esc 放弃`，回合在此暂停（不写任何会话消息）。此时插件可
调用本接口代替用户按键触发重试：

| 项 | 说明 |
|------|------|
| 返回 | `True` = 已触发重试；`False` = 当前无等待态（回合未出错、错误属 401 等确定性失败、或用户已放弃） |
| 线程 | 任意线程（内部为事件置位，非阻塞） |
| 典型场景 | 远程控制端点的"重试"按钮、监控插件在检测到限频解除后自动放行 |

```python
def _api_retry(payload):
    if ctx.request_llm_retry():
        ctx.notify("[助手] 已代为触发重试")
    return None

ctx.register_external_api("llm_retry", _api_retry)
```

等待态的退出途径：Ctrl+Y 或本接口触发重试（同一请求重发，不计轮次、不影响会话）；
Esc 放弃；提交新消息自动放弃（消息按 busy_send_mode 排队/中断接力）；
`config.llm.retry_wait_seconds` 超时（0=无限等待，默认值）。


---

## 5. Hook 事件目录

### 5.1 两种调用语义

BAWCode 的每个 hook 事件属于两种语义之一（下表标明），写处理函数前先分清：

**变换链（`call`）** —— "可以改写输入"。处理函数按 `priority` 升序依次执行
（同优先级按注册顺序），规则：

- 返回 **`None`**：表示"不参与"，不影响链条（观察、记日志的场景都用它）
- 返回 **`dict`**：与当前 payload **浅合并**后继续向后传递——后续处理函数拿到的是
  合并后的 payload；链条结束时，最终返回值为**各 dict 贡献的浅合并**（后者覆盖
  前者），非 dict 结果（如 str）原样返回并遮蔽前序 dict 贡献
- 返回 **非 dict 非 None** 的值：会取代前序贡献成为最终结果，只在"整段接管"场景使用
- 全部返回 None（或没有处理函数）：调用方使用 `default`，即回落本地默认逻辑

**观察链（`collect`）** —— "多方各自贡献"。每个处理函数独立收到**原始 payload**
（互相之间不合并），所有非 None 返回值按执行顺序收集成列表交给调用方。
处理函数抛异常只记日志，不影响自己和其他处理函数。

### 5.2 事件总表

#### 用户参与型能力（对应 `config.external_apis` 同名映射；变换链）

| 事件 | 触发时机 | payload | 生效返回值 |
|------|----------|---------|-----------|
| `prompt_refine` | 任务提示完善阶段 | `{prompt: str}` | `{prompt: str}` 或 `str` |
| `plan_generate` | 计划生成（llm 外部接口）；`write_plan`/`update_plan` 工具也会携带 `{plan, mode}` 触发 | `{prompt}` 或 `{plan, mode}` | `{title, content}` |
| `plan_confirm` | 计划确认回调 | `{plan, action, feedback}` | `{...}`（调用方解读） |
| `step_generate` | 步骤生成 | `{steps: [str]}` | `{steps: [str]}` —— 替换待写入的步骤 |
| `step_update` | 步骤状态变更通知 | `{step: {...}}` | 无消费方（通知型） |
| `memory_write` | 长期记忆落盘前 | `{memory, path, agent_md_path, project_md_path}` | 无消费方（旁路，如同步到远端） |
| `memory_read` | 长期记忆读取 | `{path, agent_md_path, project_md_path}` | `{memory: {...}}` —— 整体替换读取结果 |
| `rag_add` | RAG 文档加入 | `{text, source}` | 无消费方（外部向量库可在此落库） |
| `rag_query` | RAG 检索 | `{query}` | `{content: str}` —— 替换检索结果 |
| `computer_use` | `computer_use` 工具调用 | `{action, params}` | 结果对象（dict/str），None 则工具返回"未配置外部接口" |
| `llm_request` | 每次 LLM 请求前 | `{messages, tools, payload}` | chat 响应结构（`{content, tool_calls, ...}`）—— **整段接管**该次请求；None 走正常请求 |
| `tool_confirm` | 工具确认面板弹出前 | `{name, arguments}` | `{action: "allow_once"}` / `{action: "allow_always"}` / `{action: "deny", reason: str}` —— 代答；None 交回 TUI 确认面板 |

#### 生命周期事件

| 事件 | 语义 | 触发时机 | payload | 生效返回值 |
|------|------|----------|---------|-----------|
| `session_start` | collect | 会话初始化完成（启动/`/new`/`/resume`） | `{project_id, session_id, workspace}` | 无消费方 |
| `before_turn` | call | 回合开始、用户消息入档前 | `{user_text: str}` | `{user_text: str}` —— 改写本轮用户输入（返回空/空白视为不参与） |
| `after_turn` | collect | 回合收尾（正常/中断/异常都触发） | `{user_text, response, session_id}` | 无消费方 |
| `stream_delta` | collect | 流式输出增量（每 token 触发，高频率） | `{kind, piece}` | 无消费方 |
| `turn_status` | collect | 回合状态/阶段变化 | `{status}` 或 `{phase}` | 无消费方 |
| `before_tool` | call | 工具确认通过、执行前 | `{name, args}` | `{args: {...}}` 改写参数；`{decision: "deny", message: str}` 拒绝执行（拒绝文案回传给模型）；None 原样执行 |
| `after_tool` | collect | 工具执行完成（含出错） | `{name, args, output, elapsed}` | 无消费方 |
| `context_supplement` | collect | 每回合构建上下文补充时 | `{project_id, session_id}` | `str`（非空文本）—— 以 `[插件补充]` 消息注入本回合上下文（不进会话历史，每回合重建） |

### 5.3 上下文补充的预算与礼仪

`context_supplement` 的文本每回合都会进入模型上下文，请克制：

- 单条建议 ≤ 200 字符；只注入"模型不知道且本回合需要"的信息
- 可在处理函数里读取 `ctx.settings`，让用户按需开关
- 文本会统一加 `[插件补充]` 前缀，不要自带大标题

### 5.4 实现 external_apis 扩展点

`external_apis` 是宿主为"核心能力可外置"预留的扩展点集合（HTTP URL 挂接，见 §13）。
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
普通 `register_hook` 缺省 100。变换链语义下每个返回 dict 的处理函数都合并进最终
结果，因此"想接管的 handler 返回结果，想跳过的返回 None"即可共存。

### 5.5 插件间互通

`ctx.call(event, payload, default)` / `ctx.collect(event, payload)` 让插件触发自定义
事件（事件名建议加 `<id>.` 前缀避免撞名，如 `"my-plugin.translate"`）。其他插件或宿主
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

- 命令名不区分大小写，需以 `/` 开头（缺省会自动补）
- **与内建命令同名会覆盖内建**——避免使用 `/help` `/model` 等既有名；被覆盖的命令在
  插件卸载后自动恢复
- 元数据（hint/usage/aliases）参与 `/help`、Tab 补全；`source` 固定为 `plugin:<id>`

### 6.2 参数补全

`completer` 参数（或 `ctx.register_arg_completer`）为命令注册参数补全器：

```python
def _complete_env(config, arg):
    items = [{"name": f"/deploy {e}", "hint": h, "source": "arg", "callable": True}
             for e, h in [("staging", "预发布"), ("prod", "生产")]]
    return [i for i in items if i["name"].lower().endswith((arg or "").lower())] or items

ctx.register_command("/deploy", hint="部署", handler=_deploy, completer=_complete_env)
```

`fn(config, arg) -> [{name, hint}, ...]`；`register_arg_completer` 还可为**内建命令**追加
参数建议（如给 `/model` 加别名）。插件卸载时只注销自己注册的补全器，内建的保留。

---

## 7. 模型可见工具

`ctx.register_tool` 把函数注册进工具注册表——出现在每回合的工具定义（function calling）
里，由模型按需调用，走**统一权限/确认/上下文限额管线**（与 `read`/`write` 同待遇）。

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

- **`name` 必须全局唯一**：建议 `plugin 简名_功能`（如 `mytr_translate`）。与既有工具
  同名会覆盖内建工具，插件卸载后内建恢复
- **`description` 面向模型**：写清楚"什么时候该用"，否则模型不调用或乱调用；同时注明
  限制场景可避免滥用
- `schema` 是标准 JSON Schema（`properties`/`required`）；宿主会自动注入公共
  `description` 参数（模型用它留档调用意图，执行前剥除，不进入你的函数）
- 函数按 `schema` 属性名以关键字参数调用，**形参名要与属性名一致**并给缺省值
- 权限：未列入 `policy.SAFE_TOOLS` 的新工具在 auto/manual 模式下会弹确认面板；
  用户"始终允许"后记入指纹白名单
- 返回值超长部分按 `context.inline_limit_tokens` 外置落盘——不用担心大结果撑爆上下文

---

## 8. 插件自带技能

插件目录下的 `skills/<技能名>/SKILL.md` 自动并入技能系统（渐进式披露：启动只进元数据
清单，模型经 `load_skill` 按需读正文）：

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

优先级：**项目技能（`.bawcode/skills`）> 插件技能 > 全局技能（`data/skills`）**——同名
`name` 时后扫的覆盖先扫的。格式规范（frontmatter 字段、scripts/references 约定）与
`data/skills` 完全一致。

---

## 9. 配置参考

### 9.1 配置声明与设置面板"插件"标签页

在 `plugin.json` 中声明 `config` 数组，插件即拥有**可视化配置界面**：设置面板
（`/settings`）新增"插件"标签页，列出识别到的全部插件（含禁用/失败，附原因）：

- **插件行**：`←→` 开/关插件（写盘 `config.plugins.disable` 并立即重载生效）；
  `Enter` 展开/收起该插件的配置列表（树状缩进，仅声明了 config 的插件可展开）
- **配置行**：按声明类型渲染不同控件——`bool` 勾选框（`[x]`/`[ ]`，Enter/←→ 切换）、
  `list` 左右键循环切换选项（复用设置页既有交互）、`str`/`int`/`float` Enter 后行编辑
  （int/float 带类型校验与 min/max 钳制，非法输入保留原值）
- 所有改动**即时写透**到 `config.plugins_config[<id>]` 并落盘，插件经
  `ctx.settings` 实时读取（无需重载）

声明格式（支持 `str`/`list`/`int`/`float`/`bool` 五种类型）：

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
| `key` | 全部 | 配置键名（字母开头，字母数字下划线），即 `ctx.settings` 与 `plugins_config` 里的键 |
| `type` | 全部 | `str` / `list` / `int` / `float` / `bool`；非法声明的项被丢弃（warn 日志），不影响插件装载 |
| `label` | 全部 | 设置面板显示名（缺省用 key） |
| `hint` | 全部 | 设置面板底部的说明文字 |
| `default` | 全部 | 缺省值（按类型规整；`list` 的 default 必须命中 options，否则取首项） |
| `options` | list | **必填**，非空字符串数组，左右键在其间循环 |
| `min` / `max` | int/float | 可选数值范围，写入时钳制 |

程序侧 API（UI 之外同样可用）：`plugins.declared_configs(pid)` 取声明、
`plugins.get_setting(pid, key, default)` 读、`plugins.set_setting(pid, key, value, config)`
写（按声明规整钳制后写盘）。

### 9.2 配置文件段

`data/config.json` 中与插件相关的段（均有内置缺省，可只写覆盖项）：

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

读取优先级：**声明缺省 < 旧版自定义段（如有）< `plugins_config` 持久化值**。动态行为
请在处理函数内读取 `ctx.settings`（每次访问实时重读），不要在 `setup()` 时缓存。

设计约定：`plugins_config` 只放**行为开关与参数**，不放秘密（api_key 等）——插件进程内
运行，读得到整个 config，请勿在文档中诱导用户把敏感信息交给不可信插件。

---

## 10. /plugin 命令

| 命令 | 作用 |
|------|------|
| `/plugin` | 列出全部插件：id、版本、状态（装载/禁用/失败）、来源、注册量（hooks/tools/commands）；失败插件附错误原因；末尾附当前 hook 扩展点占用概览 |
| `/plugin reload` | 重新发现并装载全部插件（先整体卸载）。开发插件时改完代码执行它，无需重启 |
| `/plugin disable <id>` | 禁用插件并**写回** `config.plugins.disable`（持久化），随后自动重载 |
| `/plugin enable <id>` | 从 disable 列表移除并重载（若清单 `enabled: false` 仍不会装载） |

状态含义：

- `[装载]` —— setup 执行成功，注册项生效
- `[禁用]` —— 因 §3 的启用优先级被跳过（error 字段说明原因）
- `[失败]` —— 装载抛异常（导入错误/setup 内错误），**已注册的部分会整体回滚**，
  不影响其他插件与主流程

---

## 11. 调试

1. **看状态**：`/plugin` —— 注册量、失败原因一目了然
2. **看日志**：`data/log/` 下按天滚动；插件日志在 `plugins.<id>` 命名空间。把
   `config.log.modules` 中 `"plugins": "debug"`（或 `{"plugins.<id>": "debug"}`）打开
   可看详细过程：

   ```json
   "log": { "level": "info", "modules": { "plugins": "debug", "hooks": "debug" } }
   ```

3. **hook 链诊断**：`hooks.list_hooks()`（可在插件里调用）返回
   `{事件: [{owner, priority, name}]}`，核对注册与优先级
4. **常见错误对照**：

   | 现象 | 原因 |
   |------|------|
   | `/plugin` 看不到插件 | 目录在 `_`/`.` 开头目录里；`id` 非法；插件系统全局关闭 |
   | 状态 `[失败]`，报 `SyntaxError` | main.py 语法错误，先 `python -m py_compile main.py` |
   | 状态 `[失败]`，报 `FileNotFoundError: 入口文件不存在` | entry 文件名拼错 |
   | 命令补全可见但执行报 no_handler | 只注册了元数据没给 handler |
   | 模型不调用你的工具 | description 不够明确；schema 与函数形参不一致（看日志"工具 X 参数错误"） |
   | hook 没触发 | 事件名拼错（对照 §5.2 总表）；变换链事件里返回了 None |
   | 改了代码 /plugin reload 不生效 | 确认装载的是这一份（global vs project 覆盖）；`__pycache__` 陈旧时可删除插件目录下缓存 |

---

## 12. 生命周期与线程模型

```
启动 main()
 ├─ Config 加载
 ├─ plugins.load()            ← 插件装载（先于会话，可收 session_start）
 │   ├─ discover()            双层目录扫描 + 覆盖解析
 │   ├─ _load_one() × N       逐个：按 requirements.txt 静默补齐依赖（见 §2.3）
 │   │                        → import 入口 → setup(ctx) → 登记注册量
 │   │                        （单插件失败：回滚其注册，继续下一个）
 │   └─ _sync_plugin_skills() 插件 skills/ 并入技能系统
 ├─ init_session()            ← session_start 事件触发
 ├─ runner 创建 + bind_runtime(app, runner)   ← 类用户操作 API 载体注入（§4.4）
 └─ 主循环 / agent 线程        ← before_turn / before_tool / after_tool / after_turn ...
```

- **装载在主线程**，但 hook 调用多发生在 **agent 后台线程**；hook 注册表有锁保护，
  `/plugin reload` 与在途回合并发是安全的（在途调用可能拿到旧快照，属预期）
- **类用户操作 API 的可用时机**：`bind_runtime` 注入后（回合调度器创建之后）`ctx.ui`
  与 `ctx.submit_turn` 才生效；此前调用返回失败提示/None，不抛异常。`/plugin reload`
  不影响已注入的运行时载体
- **重载语义**：整体卸载（按 owner 注销 hooks/commands/tools、清理 sys.modules 与
  sys.path、清空插件技能目录）→ 重新发现装载。模块级全局变量不保留——需要跨重载的
  状态请存 `ctx.storage_dir()`
- **后台资源清理（必须）**：setup 里启动线程/HTTP 服务器等长期资源的插件，务必
  `ctx.register_teardown(fn)` 注册卸载回调——卸载/重载/禁用回滚时按注册顺序执行
  （异常隔离记日志）。没有它，`/plugin reload` 后旧线程会残留且插件自身无法停止
  （模块对象已换新）。官方示例：remote-control 插件的 HTTP 服务即经 teardown 停止
- **运行时载体只读访问**：`core.plugins.runtime()` 返回 `{app, runner}`（`bind_runtime`
  注入的同一对象引用；未注入时值为 None）。可读取 `app.busy / app.streaming_msg /
  app.status` 等实时状态、`runner.busy` 判定忙闲，或 `runner.llm.cancel()` 中断在途
  请求（remote-control 的"停止"按钮即此实现）。约定只读，勿替换其中对象
- **sys.path**：装载期间插件目录会被加入 `sys.path`（便于 `import 自带模块`），卸载时
  移除。若插件以后台线程长期持有 import 引用，重载后旧模块对象仍存活，注意避免
- hook 处理函数**不要长时间阻塞**：`before_tool`/`before_turn` 在关键路径上，阻塞会
  卡住整个回合；耗时工作请放线程并及时返回

---

## 13. 兼容性：旧扩展方式

插件系统落地后，以下旧机制全部保留并**统一到 hook 链**上：

| 旧方式 | 现状 |
|--------|------|
| `config.external_apis.<事件> = URL` | 不变。启动时挂接为 `owner=external_apis`、优先级 50 的 HTTP POST 处理函数；请求体 `{"event", "payload"}`，响应 JSON 作为扩展点返回值；失败回落本地逻辑。与插件 handler 同链共存，按优先级与"None=不参与"协作 |
| `hooks.register_hook(event, fn)` 进程内注册 | 不变（签名兼容，新增 `priority/owner/name` 关键字参数）。单事件从"单槽覆盖"变为"多处理函数链"——旧行为等价于链上只有一个处理函数 |
| `data/commands/*.py` 命令插件 | 不变（`commands.load_plugins`，配置 `command_plugins`）。新插件建议迁移到本系统以获得 owner 卸载与 `/plugin` 管理 |
| `core/register.register` 工具装饰器 | 不变（新增 `owner` 参数）。内建工具 owner 为空，不受插件卸载影响 |

---

## 14. FAQ

**Q：插件能 hook 其他插件吗？**
能。`ctx.call/collect` 触发自定义事件，`ctx.register_hook` 监听任意事件（含其他插件
的自定义事件）。装载顺序按目录名排序，有依赖时在 setup 里防御性判断（对方可能未装载）。

**Q：插件能发 LLM 请求吗？**
可以：`ctx.config` 拿到宿主配置后自建请求，或用 `session.set_llm_fn` 注入的函数。建议
经宿主 `LLM` 实例（`ctx.config` + `core.llm.LLM`）以复用重试/计费统计。整段接管主对话
请用 `llm_request` 扩展点。

**Q：能改模型返回内容吗？**
`llm_request` 可整段接管；`before_turn` 可改写用户输入；`before_tool` 可改写工具参数或
拒绝工具。没有"事后改 assistant 输出"的扩展点——那是故意的（避免插件篡改对话历史）。

**Q：插件能像用户一样发消息、弹面板吗？**
能，见 §4.4：`ctx.submit_turn(text)` 等价用户发送（busy 时按设置排队/中断）、
`ctx.notify(text)` 写用户可见通知、`ctx.ui.confirm/choose/line` 弹交互面板（仅 agent
线程）。注意 submit_turn 的自激风险——循环触发自己的 hook 请加冷却。

**Q：插件冲突怎么排查？**
`/plugin` 看 owner 与注册量 → `hooks.list_hooks()` 看同一事件上有哪些 owner → 临时
`/plugin disable <id>` 二分定位。

**Q：想做图形界面/悬浮窗？**
插件可自起线程/子进程（如 PySide6），但注意 TUI 终端独占前台：GUI 进程应是独立窗口且
不抢终端焦点；退出前清理线程。

**Q：如何分发？**
把插件目录打成 zip 即可。接收方解压到 `data/plugins/`（全局）或
`.bawcode/plugins/`（项目）。发布前在 `plugin.json` 写明 `version` 与 `description`，
并提供 `config.plugins_config` 的建议配置示例。若插件有第三方依赖，附上
`requirements.txt`（见 §2.3），宿主会在首次装载时自动补齐，接收方无需手工安装。

---

*本指南对应实现：`core/plugins.py`（装载器）、`core/hooks.py`（hook 链）；
示例插件：`data/plugins/hello-plugin/`（入门）、`data/plugins/rag/`（项目 RAG 知识库，
自核心剥离的真实案例：同名工具迁移 + context_supplement 上下文补充 + external_apis
接管语义保留）、`data/plugins/remote-control/`（远程控制：后台 HTTP 服务 + register_teardown
+ 类用户操作 API + tool_confirm 代答的综合案例）；
端到端测试：`develop/test_plugins_e2e.py`、`develop/test_rag_plugin.py`、`develop/test_remote_control_e2e.py`。*

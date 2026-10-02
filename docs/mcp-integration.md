# BAWCode MCP 功能方案报告

> 2026-10-02 定稿 · 依据对 qwen-code（TS）、hello_agents（Python）两份参考实现的分析 + BAWCode 现状实测
> 结论先行：采用官方 `mcp` Python SDK + 专用事件循环线程同步桥接；v1 聚焦工具核心（stdio + Streamable HTTP）。

---

## 1. 背景与目标

MCP（Model Context Protocol）是 Anthropic 发起的开放协议，让 LLM 应用以统一方式连接外部工具服务器（fetch、playwright、数据库、git 等）。给 BAWCode 增加 **MCP 客户端（host）能力**后，外部 MCP 服务器的工具将直接桥接进现有工具循环，无需逐个自研工具即可接入整个 MCP 工具生态。

目标边界：

- BAWCode 作为客户端连接用户在配置中声明的 MCP 服务器，把服务器的 **tools** 桥接为本机工具；
- 工具调用走既有的 LLM function calling → 权限确认 → 执行 → 上下文管理全链路；
- 不改变 BAWCode 纯同步 + threading 的整体模型（MCP 的 asyncio 局限在一个守护线程内）。

## 2. 参考实现分析

参考代码位于 `新建文件夹/`：`qwen/qwen-code`（TypeScript CLI，fork 自 gemini-cli 架构）与 `hello_agents/`（Python 教学框架）。

### 2.1 qwen-code —— MCP 客户端主参考

运行时代码在 `packages/core/src/tools/`（`mcp-*` 前缀），`packages/core/src/mcp/` 只放 OAuth。

**模块划分**

| 模块 | 职责 |
|---|---|
| `mcp-client.ts` (2626 行) | 每服务器一个 `McpClient`：connect/disconnect/discover；`createTransport` 传输工厂 |
| `mcp-client-manager.ts` (3294 行) | `McpClientManager`：`clients: Map` 持有全部客户端；批量/增量发现、健康监测、预算护栏 |
| `mcp-status.ts` | 全局状态注册表：三态状态 + lastError + 监听器 |
| `mcp-tool.ts` (1409 行) | `DiscoveredMCPTool`：LLM 面向的工具包装、结果转换、重连/重放 |
| `mcp-retry.ts` | `retryWithBackoff`（默认 2 次重试、200ms 基数指数退避；瞬时网络错误判定；401/403 与 JSON-RPC -32601/-32600/-32602 永不重试） |
| `mcp-discovery-timeout.ts` | 发现超时：stdio **30s** / 远程 **5s** / 按服务器 `discoveryTimeoutMs` 夹在 [100ms, 300s] |

**状态机**：`MCPServerStatus = DISCONNECTED / CONNECTING / CONNECTED`——**没有 FAILED 态**，失败 = DISCONNECTED + 可取回的 lastError（连上后自动清除）。状态变更通过监听器列表广播，驱动 Footer 状态徽标与 `/mcp` 对话框。

**生命周期**：

- `connect()`：清残留 OAuth/错误态 → CONNECTING → 建传输 → 注册 capabilities 与 roots/list 处理器 → 带超时 connect → 抓取服务器 `instructions` → CONNECTED；失败记录 lastError 回 DISCONNECTED。
- `discover()`：并发 list prompts/resources/tools，各自重试后失败吞掉返回 `[]`（容忍 -32601 Method not found）；全部为空视为失败。
- `disconnect()`：Streamable HTTP 先 `transport.terminateSession()`（DELETE `mcp-session-id`，**2s 上限**——不设上限会被无响应服务器挂住 teardown，且单会话服务器会拒绝后续重连），再 close。
- 批量发现 best-effort：单服务器失败只断开自己、释放预算槽、继续兄弟服务器，绝不整体失败。

**传输**（`createTransport` 分派）：

- **stdio**：`StdioClientTransport({command, args, env, cwd, stderr: 'pipe'})`；env = 清洗过的继承环境（Windows PATH/Path 合并）叠加配置 env；cwd 必须存在；stderr 走管道与协议 stdout 隔离，仅 debug 时打日志。"`stdin was unexpectedly closed`" 一类传输死亡不特判字符串，而是结构性处理：`onerror` 捕获进 lastError + 健康监测重连。
- **Streamable HTTP**（`httpUrl`）：undici fetch 专用 dispatcher（无 header/body 超时），外加兼容垫片：保留 401 + `www-authenticate` 供 OAuth 探测；把可选 GET-SSE 流上的 **400/404 改写成合成 405**，让 SDK 回落到 POST-only 模式（Spring AI 400、Express 404 两类真实兼容案例）。
- **SSE**（`url`）：`SSEClientTransport` + OAuth Bearer。
- WebSocket（`tcp` 字段）：配置与记账已留位，`createTransport` **尚未实现**。

**超时体系（三层分离，关键设计）**：

| 层 | 默认值 | 说明 |
|---|---|---|
| 发现超时 | stdio 30s / 远程 5s | 建连 + 枚举，短 |
| 工具调用超时 | 10 min（`MCP_DEFAULT_TIMEOUT_MSEC`） | 每次调用，长，可按服务器覆盖 |
| 空闲超时 | 5 min | 有 progress 通知就重置；防死服务器挂调用 |

**工具桥接**（`mcp-tool.ts` + `tool-name-utils.ts`）：

- 注册名 = `normalize("mcp__" + server + "__" + tool)`：长度 ≤63 且匹配 `^[A-Za-z][A-Za-z0-9_-]*$` 则原样；否则非法字符替换为 `_`、不以字母开头则加 `tool_` 前缀、截断后追加 `_` + FNV-1a(36bit) base36 7 位哈希后缀。**注册名面向 LLM/权限，原始 serverToolName 才用于实际 callTool**——两者分离。
- schema 来自 `tools/list` 的 `inputSchema`（缺省补 `{type:"object",properties:{}}`），另补抓 annotations（`readOnlyHint/destructiveHint/idempotentHint/openWorldHint`）与 `_meta.ui`；`_meta.ui.visibility` 不含 `model` 的工具不下发（SEP-1865）。
- `readOnlyHint` → 工具分类 Kind.Read（权限与安全重放判定用）。
- `includeTools`（精确或 `name(args)` 形式）与 `excludeTools`（精确，优先级更高）做服务器内过滤。
- 结果转换：`text` → 文本；`image/audio` → 多模态 inlineData + 文字说明；`resource`（内嵌）→ 文本或 blob；`resource_link` → `"Resource Link: <title> at <uri>"`；`structuredContent` 无文本块时前置为 JSON 文本；`isError: true` → 结构化 MCP_TOOL_ERROR。
- 单工具输出上限 500k 字符（全局工具的 10 倍），超限落盘附路径。

**可靠性与重连**：调用报错时按信号分类——状态已 DISCONNECTED、HTTP 404 死会话、或消息命中连接错误模式表（ECONNREFUSED/ECONNRESET/"connection closed"/"transport closed"/"session expired" 等）→ 判定可重连，最多 **3 次**；**安全重放门禁**：仅 `trust` 且（readOnlyHint 或 idempotentHint）才自动重放调用，否则只修复连接并抛"调用可能已执行，勿自动重试"。健康监测：30s 间隔 × 连续 3 次失败 → 5s 后重连（计时器 unref，不阻进程退出）。

**配置与信任**：

- `MCPServerConfig` 字段：stdio `command/args/env/cwd`；远程 `url`（SSE）/`httpUrl`（streamable）/`tcp`；公共 `headers/timeout/trust/description/includeTools/excludeTools/oauth/discoveryTimeoutMs/scope/alwaysLoadTools`。
- 来源优先级（低→高）：用户设置 < 项目 `.mcp.json`（scope=project）< 工作区/系统设置 < 命令行注入。`.mcp.json` 要求顶层 `"mcpServers"` 对象，容忍 Claude 的 `type` 判别式格式，纯读取从不连接，格式错误非致命。
- **项目级服务器须信任审批**：project/workspace scope 的服务器进入 pending，批准记录持久化在 `~/.qwen/mcpApprovals.json`（`{项目根: {服务器名: {hash, status}}}`）；hash 为配置 JSON（递归排序键、剔除 scope/description 等非行为字段）的 SHA-256——**改配置即改哈希即回到待批准**。审批前不 spawn、不建传输、不进健康检查。YOLO 模式跳过门禁。

**其他**：MCP prompts 注册为斜杠命令（撞名改 `server_name` 前缀）；resources 注册进注册表 + 专用 `read_mcp_resource` 工具（`{server_name, uri}`，非 trust 需确认，逐回合 18MB blob 预算）；服务器 `instructions` 从 initialize 抓取对外暴露；依赖 `@modelcontextprotocol/sdk ^1.30` 并行 v2 拆分包，双时代兼容。

### 2.2 hello_agents —— 次参考（架构模式，非 MCP 代码）

**全文检索证实：hello_agents 没有任何 MCP 客户端实现**（包体、docs、examples、依赖均无 mcp/modelcontextprotocol 痕迹）。其价值在于围绕"远程工具服务器"设计好了配套设施——docs 里熔断器的动机示例就是"MCP 服务器宕机"：

- **ToolResponse 结构化响应**：`status(SUCCESS/PARTIAL/ERROR) + text(LLM 只见这个) + data/stats/context(结构化留档)`；错误码分类表（含 CIRCUIT_OPEN/NETWORK_ERROR/TIMEOUT）。
- **工具级熔断器**：注册表的 `execute_tool` 是唯一咽喉点——先查熔断状态，执行后按结果记账；连续 3 次 ERROR 熔断，300s 恢复窗口，熔断期返回 CIRCUIT_OPEN 错误文本喂回 LLM。防的是 agent 循环捶死服务器。
- **subagent tool_filter**：子代理运行时临时摘除不允许的工具（白名单/黑名单两种模式）。
- **LLM 参数类型矫正**：`_convert_parameter_types` 把模型发来的字符串实参按 schema 声明矫正成 number/integer/boolean（OpenAI 兼容端点高频问题），矫正失败保留原值。
- 会话持久化存 tool-schema 哈希、重载时漂移告警——对动态增减的 MCP 工具同样适用。

### 2.3 对 BAWCode 的启示清单

1. 三层超时分离（发现短 / 调用长 / 空闲可重置）——直接采纳（v1 先做前两层）。
2. 三态状态机 + lastError 表，不做 FAILED 态——直接采纳。
3. 注册名规范化（63 字符、字符集、哈希后缀）+ 注册名/原始名分离——直接采纳。
4. 发现 best-effort、单服务器失败不拖累整体——直接采纳。
5. 结果块统一转文本（v1 不做多模态）——采纳，图片转占位说明。
6. 熔断器、健康监测自动重连、include/exclude 过滤、重试退避——Phase 3。
7. 项目级 `.mcp.json` + 配置哈希信任审批——Phase 2（v1 只认 data/config.json，天然免审批问题）。
8. prompts→斜杠命令、resources→read 工具、instructions 注入——Phase 2。
9. hello_agents 的类型矫正——v1 采纳（MCP schema 类型声明更严格，模型实参常是字符串）。
10. ToolResponse 的"LLM 只见 text"——BAWCode 现有工具即此范式（返回 str），无需引入。

## 3. BAWCode 现状与集成点（实测确认）

**工具注册表 `core/register.py`**：扁平字典 `_registry`，装饰器/编程式两种注册（subagent 已用编程式），`get_tool_defs()` 导出 OpenAI function 格式并**自动给每个工具 schema 注入公共 `description` 参数**（执行前由 `LLM.execute_approved_tool` 剥除）——MCP 的 inputSchema 直接入册即可搭车，零改动。

**分发链**：`workflow._tool_loop`（workflow.py:369）→ 从实参弹出 `description` → `policy.evaluate` 分流（ALLOW 直接执行 / CONFIRM 走确认桥）→ `LLM.execute_approved_tool`（llm.py:656，`register.has_tool` 校验 → `register.call` → 异常转错误文本）→ `memory.add_tool_result`（超限外置 toolstore 附落盘指针）→ 回合末 `memory.finalize_turn` 按 `context.tool_whitelist`（精确工具名）剥离非白名单结果。

**权限 `core/policy.py`**：MODE_AUTO/MANUAL/FULL；`_evaluate` 顺序：FULL→ALLOW；项目 allowlist 规则（`fingerprint`/`similar_fingerprint` 前缀 `|*` 通配）；SAFE_TOOLS 白名单；兜底 CONFIRM。**未知工具天然落 CONFIRM——MCP 工具零改动即获得安全默认**。"始终允许"走 `policy.add_always_allow` 通用指纹路径。

**配置 `core/config.py`**：`default_config()` 为权威 schema，用户文件深合并其上——新增 `mcp` 节对老配置零迁移。访问约定 `(config.data or {}).get("mcp", {})`；解析器模板照抄 `subagent.subagent_cfg`（默认值+钳制集中一处）。无热重载。

**生命周期 `main.py`**：启动序 `main()` 中 `subagent_mod.register_tools(config)`（main.py:519）之后是 MCP 启动挂点；退出清理在 runner join 附近（main.py:552）。回合级 `bind/unbind_runtime` 模式见 subagent（MCP 是进程级常驻，无需回合绑定）。

**UI `core/ui.py`**：工具调用渲染为 `⚙ <名字> ✅/❌ · 摘要` 双色节点——MCP 工具注册后自动获得渲染，无需新代码。实时状态槽参照 `subagent_stream` 范式（`_tree_signature` 加字段 + `_build_tree` 尾部加节点 + 颜色键入 `DEFAULT_COLORS`）。命令注册照抄 `/agents`（main.py `_register_commands`）。

**确认面板**：`show_confirm_form`（ui.py:2092）按 name/args 通用渲染——MCP 工具确认零改动。

**失败启发**：`tools.tool_failure_hint`（tools.py:483，模式表 :466）子串匹配定 ✅/❌——MCP 错误文本需带可命中前缀（实现里统一加 `MCP 工具返回错误:` / `MCP 调用失败:`）。

**依赖现状**：requirements 为 openai/rich/tiktoken/wcwidth/prompt_toolkit/pyyaml/PySide6；纯同步 threading，无 asyncio/httpx/mcp。

## 4. 总体设计

### 4.1 实现路线决策

| 维度 | 官方 `mcp` Python SDK（选定） | 自研同步 stdio 客户端 |
|---|---|---|
| 传输支持 | stdio / SSE / Streamable HTTP | 仅 stdio |
| 协议维护 | 官方包跟进（auth/elicitation/batch 等演进免维护） | 自行跟 spec：握手、capabilities、分帧、progress |
| 依赖代价 | +`mcp` 包（连带 anyio/httpx/pydantic 等） | 零新增 |
| 与现有代码契合 | 需 asyncio↔sync 桥接层（约百行，模式成熟） | 完全贴合 |
| 远程服务器 | 支持 | 不支持 |

选 SDK 的理由：MCP 生态本地服务器（npx/uvx）确实占大头，但远程 HTTP 服务器增长快；协议在快速演进，自研长期跟进成本高；桥接层一次性成本可控且模式成熟。

### 4.2 架构（新文件 `core/mcp.py`）

```
主线程(同步)                      MCP 事件循环线程(daemon)
────────────                      ────────────────────────
mcp.register_tools(config) ──→ 启动 loop 线程 + 后台发现线程
                                     │ asyncio loop
register.get_tool_defs()  ←── 逐服务器 connect → list_tools → register.register(...)
                                     │
LLM.execute_approved_tool            │
  └ register.call(mcp__s__t) ──→ submit(asyncio.wait_for(session.call_tool(...), t))
                                     └ CallToolResult → 转文本 → str 返回
/mcp 命令 ←── statuses()（纯内存表读取）
mcp.shutdown() ──→ 逐服务器 aclose（带超时）→ loop stop
```

- 模块级状态（照 subagent.py 范式）：`_STATE: {server: {status, last_error, transport, tools}}` + `_RUNTIME: {loop, thread, handles}`。
- 同步桥接：`asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)`；**调用超时用协程内 `asyncio.wait_for` 实现**（真实取消不留悬挂协程），外层 `.result()` 只兜底。
- 每服务器一个 handle：`AsyncExitStack`（传输 + ClientSession 的长生命周期上下文）在 loop 线程内进出；session 方法只允许在 loop 线程调用。
- Windows 注意：daemon 线程的 `asyncio.new_event_loop()` 默认即 Proactor（subprocess 可用）；无信号处理需求（signal 只在主线程派发）。

### 4.3 工具桥接

- 命名：`mcp__<server>__<tool>`，按 qwen 算法规范化（≤63 字符 + `^[A-Za-z][A-Za-z0-9_-]*$`，否则替换 + `tool_` 前缀 + FNV-1a36 base36 7 位后缀）；wrapper 闭包捕获原始工具名用于 callTool。
- schema：服务器 `inputSchema` 规整为 `{type:"object", properties, required}` 后入册；`get_tool_defs()` 自动注入 description 参数。
- 实参矫正：按 schema properties 把 string 实参矫正为 number/integer/boolean（失败保留原值）。
- 执行结果统一转文本：text 直连；image/audio 占位说明；resource_link 一行链接；内嵌 resource 取 text 否则占位；`isError` 加 `MCP 工具返回错误:` 前缀；无 content 用 `structuredContent` 的 JSON；空结果兜底文案。
- 调用异常：转 `MCP 调用失败(<server>): <err>` 文本喂回 LLM，同时状态表记 DISCONNECTED + last_error（供 `/mcp` 诊断与手动重连）。
- include_tools/exclude_tools 在发现时过滤（exclude 优先）。

### 4.4 配置（`data/config.json` 新 `mcp` 节）

```jsonc
"mcp": {
  "enabled": false,                    // 总开关，默认关
  "servers": {
    "<名字>": {
      "enabled": true,
      "command": "uvx", "args": ["mcp-server-fetch"], "env": {}, "cwd": "",   // stdio
      "url": "", "transport": "", "headers": {},                               // http/sse；transport 留空自动推断
      "timeout": 600,                  // 工具调用超时（秒），0 用全局
      "include_tools": [], "exclude_tools": []
    }
  },
  "discovery_timeout_stdio": 30,       // 发现超时（秒）
  "discovery_timeout_http": 5,
  "call_timeout": 600,                 // 全局调用超时（秒）
  "auto_reconnect": true               // v1 仅保留字段语义：调用失败自动重连一次（可关）
}
```

stdio 的 env 采用 SDK 默认安全环境 + 用户 env 覆盖的合并策略（不整体替换，避免丢 PATH/SystemRoot）。

### 4.5 权限与安全

- MCP 工具默认 CONFIRM（现状兜底，零改动）；`allow_always` 走现有通用指纹。
- 为 MCP 工具加**专用指纹分支**：`mcp__<server>__<tool>`（不含参数）——规则可读，且 `mcp__<server>__` 前缀通配可一键放行整个服务器；`similar_fingerprint` 的 `|*` 机制兼容。
- SAFE_TOOLS 不动（v1 不按 readOnlyHint 自动放行，列入 Phase 2）。
- 安全边界：config.json 里的 command 即代码（与 execute_command 同级信任，本机配置可接受）；stdio 子进程 stderr 走管道不污染 TUI；退出时清理子进程树。

### 4.6 生命周期

- 启动：`mcp.register_tools(config)` 在 subagent 注册之后调用——开 loop 线程 + 逐服务器后台连接发现（不阻塞 TUI 启动）；工具注册进 register 后下一轮 `get_tool_defs()` 自动可见（动态出现的窗口期内模型若提前调用，`has_tool` 校验返回"工具不存在"，安全）。
- 退出：`mcp.shutdown()`——逐服务器带超时 aclose（Streamable HTTP 由 SDK 关闭会话）、超时则放弃（daemon 线程不阻退出）。
- 无回合级绑定需求（进程级常驻）。

### 4.7 UI 与命令

- 工具调用/确认：零改动自动获得。
- 树尾部状态行：`app.mcp_status_line` 字符串槽（照 subagent_stream 的槽位机制：`_tree_signature` 计入 + `_build_tree` 尾部渲染），内容如 `⧉ MCP fetch✔ 3工具 · db✘ spawn ENOENT`；`DEFAULT_COLORS` 增 `mcp` 键（主题安全），单一颜色降低视觉风险。
- `/mcp` 命令：无参=列表（状态/传输/工具数/last_error）；`/mcp reconnect <名字>` 手动重连；`/mcp tools [名字]` 列工具。

### 4.8 上下文与子代理

- MCP 工具默认**不在** `context.tool_whitelist` → 回合末照常剥离（与内置工具行为一致）；用户可按名加入白名单保留。
- 子代理默认可见（走 `get_tool_defs`，`_SUBAGENT_EXCLUDED_TOOLS` 不动）；如需隔离后续在 subagent 过滤器加前缀规则。

## 5. 分阶段实施路线

**Phase 1（v1，本次实施）**

1. `core/mcp.py` 新建：loop 线程桥接、连接/发现/注册、调用包装、状态表、shutdown、reconnect。
2. `core/config.py`：`default_config()` 加 `mcp` 节。
3. `core/register.py`：`get_tool_defs()` 迭代改快照（后台线程注册期间的并发安全）。
4. `main.py`：启动挂钩、退出挂钩、`/mcp` 命令。
5. `core/policy.py`：MCP 专用指纹分支。
6. `core/ui.py`：`mcp` 颜色键 + 状态行槽位。
7. `requirements.txt` / `pyproject.toml`：+`mcp`。
8. e2e：FastMCP 起 mock stdio 服务器，走真实管线验证全链路。

**Phase 2**：项目级 `.mcp.json` + 配置哈希信任审批；MCP prompts → 斜杠命令；`read_mcp_resource` 工具；服务器 instructions 注入；readOnlyHint 接入 is_safe_call。

**Phase 3**：熔断器（借 hello_agents）、健康监测自动重连、重试退避、发现增量 reconcile、多模态结果外置（图片落盘附指针，复用 toolstore 范式）。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 同步库引入 asyncio 的停机顺序 | daemon loop 线程；shutdown 先带超时 aclose 再放弃；不阻进程退出 |
| Windows stdio 子进程清理 | SDK stdio 上下文负责收尾；aclose 超时兜底；必要时 Phase 3 加 taskkill /T 收尸 |
| GLM/DeepSeek 兼容 | 工具名规范化 ≤63 字符；schema 规整；实参类型矫正；tool_choice 固定 auto 现状保持 |
| 上下文膨胀（服务器工具多） | 默认回合末剥离；include/exclude 过滤；`/mcp` 展示工具计数 |
| 配置即代码的安全边界 | v1 只认 data/config.json（本机信任域）；项目级配置与审批绑定在 Phase 2 |
| 后台注册与 `get_tool_defs` 并发迭代 | register 快照迭代（一处一行改动） |
| 错误文案误导 ✅/❌ 启发 | MCP 失败文本统一可命中前缀 |

## 7. 验收口径（e2e，按既有取向）

用 `mcp` SDK 的 FastMCP 起本地 mock stdio 服务器（echo / add(int,int) / always_fail 三个工具），走**真实管线**：

1. 启动 `mcp.register_tools` → 状态变 CONNECTED，`register.get_tool_defs()` 出现 `mcp__` 工具且 schema 含注入的 description 参数；
2. `register.call` 直调：echo 回显、add 得数值（验证类型矫正）、always_fail 得错误前缀文本；
3. `policy.evaluate` 对 MCP 工具默认 CONFIRM、allowlist 规则命中后 ALLOW；
4. `memory.finalize_turn` 后非白名单 MCP 结果被剥离；
5. `/mcp` 列表状态正确；kill 服务器进程后调用得到失败文本并可 reconnect 恢复。

## 8. 参考资料

- qwen-code：`新建文件夹/qwen/qwen-code/packages/core/src/tools/mcp-{client,client-manager,status,tool,retry}.ts`、`packages/core/src/mcp/`（OAuth）、`docs/design/mcp-*.md`、`docs/users/features/mcp.md`
- hello_agents：`新建文件夹/hello_agents/tools/{registry,circuit_breaker,response,tool_filter}.py`、`core/agent.py`
- MCP Python SDK：`mcp`（pypi），客户端入口 `mcp.client.stdio` / `mcp.client.streamable_http` / `mcp.client.sse`，服务端 FastMCP 用于 e2e mock

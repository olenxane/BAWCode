# BAWCode

芝士一个一个一个一个终端 AI 编码 Agent，使用python实现，使用OpenAI兼容协议


## 神奇功能

- **乱七八糟的模型路由**：providers 按模型归属自动路由
  api_key/base_url，plan/review 跑强模型、code 跑大肥鱼，一个回合内可多模型协作
- **内置结构感知 RAG**：tree-sitter 按代码块切片（类/函数/注释优先召回），关键词 +
  embedding 双模式，随用户消息主动注入，开箱即用+
- **工具结果全生命周期管理**：完整输出外置磁盘、行内只留摘要与取回路径；白名单
  工具保留原文、回合末剥离其余，目前来看上下文的管理效果还是不错的
- **防误删机制**：shell 删除命令硬拦截，文件删除一律进项目回收站；每回合文件快照，
  会话树里可撤回到某一轮消息，md5校验防止修改冲突
- **防空转熔断**：重复调用工具无进展时开始计数、计满才终止，正常推进无轮次上限——替代
  固定 max-iterations 的一刀切，既防烧 拉闸，防止模型吃白饭
- **两层重试**：429/5xx/网络超时等瞬态错误按配置自动重试，401 等确定性失败直接报错；自动重试无果还能ctrl+Y重试请求
- **神奇的工作流引擎**：可自定义工作流管线，自行配置提示词等各项参数
- **插件系统**：提供了一套可能较丰富的接口可供插件调用，拓展性较强

### 神奇小特性

- **远程控制**：可以通过内置插件进行远程控制
- **粘贴功能**：可直接ctrl+V粘贴，支持超长文本、图片和文件
- **食食物着为俊杰**: 自动识别用户情绪，想骂gai时让AI哄一下你的情绪


## 内置插件（data/plugins/）

| 插件 | 功能 |
|------|------|
| `rag` | 结构感知 RAG：tree-sitter 按语言切片（类/函数/注释优先），关键词 + embedding 双模式召回，主动注入上下文 |
| `remote-control` | 能够在浏览器中随时随地实时操作终端，或者说是个WebUI |
| `qbridge` | 适配OneBotv11正向WS，可以直接在QQ里coding |
| `emotion-detector` | 用户消息情绪识别，情绪不佳时让AI主动哄你 |

## 安装与运行

```bash
pip install -r requirements.txt   # 或按需：核心依赖缺一即崩，可选组缺失仅降级
python main.py                    # 进入 TUI
```

可选功能组（见 `pyproject.toml` `[project.optional-dependencies]`）：
`gui`（工作流编辑器）/ `webfetch`（网页抓取）/ `rag`（结构索引）/ `qbridge`（QQ 桥）。

## 配置

配置文件 `data/config.json`（首次启动生成）。顶层段：

| 段 | 用途 |
|------|------|
| `providers` / `active_provider_id` / `active_model_id` | 供应商与模型（OpenAI 兼容 base_url） |
| `task_models` | plan / code / review 分角色模型 |
| `llm` | `retry_times` / `retry_delay` / `retry_wait_seconds` / `stream` 等 |
| `ui` | 主题、busy_send_mode（排队/中断）、访问模式等 |
| `context` | 工具结果白名单、行内 token 上限、外置目录、压缩阈值 |
| `memory` | 记忆目录、会话存储位置 |
| `workflow` / `subagent` / `mcp` / `snapshot` / `skills` / `plugins` | 各子系统开关与参数 |
| `external_apis` | 用户参与型 hook 的外部接管映射（如 llm_request 指向自建服务） |

## 常用命令

```
/help /tools          帮助与工具清单          /model /balance   切换模型、查余额
/mode                 auto/manual/full 切换   /settings         设置页（主题/快捷键/压缩等）
/plan /steps /refine  计划与步骤              /undo             回滚上一回合文件快照
/new /resume /rename  会话管理                /agents           子代理记录
/plugin               插件管理（reload/启停） /mcp              MCP 服务器状态与重连
/workflow             工作流切换与编辑         /skill            技能查看与重扫
/rag                  RAG 状态 / 建索引 / 开关（插件）
```

## 内置工具（节选）

读写编辑：`read` `read_image` `write` `edit_file` `multi_edit` `delete_file`（进回收站）；
检索：`search` `glob` `list_directory`；执行：`execute_command` `run_program`；
网络：`webfetch`；规划：`write_plan` `update_plan` `generate_steps` `update_step_status`；
记忆：`write_memory` `update_memory` `read_memory` `delete_memory`；
技能与协作：`load_skill` `ask_user` `task` `query_subagent`；`computer_use`。
MCP 与插件工具按注册动态追加。

## 项目结构

```
main.py               入口：TUI 主循环 / 无头模式 / AgentRunner 后台调度
core/                 核心模块（llm / loop / tools / policy / memory / ui / plugins /
                      mcp / subagent / workflow / snapshot / keyinput / config …）
gui/workflow_editor.py  PySide6 工作流可视化编辑器（python -m gui.workflow_editor）
data/                 运行时数据：config.json、sessions/、memory/、snapshots/、
                      toolcalls/、plugins/、workflows/、allowlist/
docs/                 插件开发、MCP 集成、QQ 桥接文档
develop/              测试基建：mock LLM 服务器 + 真实管线 e2e 套件（不入库约定见 CHANGELOG）
```

## 文档

- [插件开发指南](docs/plugin-development.md) —— hook 事件目录、PluginContext API、示例
- [MCP 集成](docs/mcp-integration.md) —— 传输配置、命名规范、重连语义
- [QQ 桥接](docs/qbridge.md) —— OneBot 对接与消息路由
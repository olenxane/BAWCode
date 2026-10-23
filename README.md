# BAWCode

芝士一个一个一个一个终端 AI 编码 Agent，使用python实现，使用OpenAI兼容协议


## 神奇功能

- **乱七八糟的模型路由**：providers 按模型归属自动路由
  api_key/base_url，plan/review 跑强模型、code 跑大肥鱼，一个回合内可多模型协作
- **内置结构感知RAG插件**：tree-sitter 按代码块切片（类/函数/注释优先召回），关键词 +
  embedding 双模式，随用户消息主动注入，开箱即用+
- **工具结果全生命周期管理**：完整输出外置磁盘、行内只留摘要与取回路径；白名单
  工具保留原文、回合末剥离其余，目前来看上下文的管理效果还是不错的
- **防误删机制**：shell 删除命令被拦截并改为移入项目回收站，文件不会真正删除，需 /clear-trash 才彻底清空；每回合文件都有存快照，会话树里可撤回文件和会话到某一轮消息
- **防空转熔断**：重复调用工具无进展时开始计数、计满才终止，正常推进无轮次上限——替代
  固定 max-iterations 的一刀切，既防烧 拉闸，防止模型吃白饭
- **尽力帮你恢复连接的重试**：429/5xx/网络超时等瞬态错误按配置自动重试，401 等确定性失败直接报错；自动重试无果还能ctrl+Y重试请求
- **神奇的工作流引擎**：可自定义工作流管线，自行配置提示词等各项参数
- **拓展性很强的插件系统**：提供了一套可能较丰富的接口可供插件调用，可以做各种神奇小插件

### 神奇小特性

- **远程控制**：可以通过内置WebUI的插件进行远程控制
- **粘贴功能**：可直接ctrl+V粘贴，支持超长文本、图片和文件
- **食食物着为俊杰**: 情绪插件能够自动识别用户情绪，想骂gai时让AI哄一下你的情绪

## 内置插件（data/plugins/）

| 插件 | 功能 |
|------|------|
| `rag` | 结构感知 RAG：tree-sitter 按语言切片（类/函数/注释优先），关键词 + embedding 双模式召回，主动注入上下文 |
| `remote-control` | 能够在浏览器中随时随地实时操作终端，或者说是个WebUI |
| `qbridge` | 适配OneBotv11正向WS，可以直接在QQ里coding |
| `emotion-detector` | 用户消息情绪识别，情绪不佳时让AI主动哄你 |
| `web-search` | 必应网络搜索工具：关键词 → 真实浏览器头请求结果页 → 结构化结果（标题/链接/摘要） |

## 安装与运行

```bash
pip install -r requirements.txt   # 或按需：核心依赖缺一即崩，可选组缺失仅降级
pip install -e                    # 项目目录下执行，后续可在任意目录通过bawcode命令启动
python main.py                    # 进入 TUI
```

可选功能组（见 `pyproject.toml` `[project.optional-dependencies]`）：
`gui`（工作流编辑器）/ `webfetch`（网页抓取）/ `rag`（结构索引）/ `qbridge`（QQ 桥）。

## 常用命令

```
/help /tools          帮助与工具清单          /model             切换模型
/mode                 auto/manual/full 切换   /settings         设置页（主题/快捷键/压缩等）
/plan /steps /refine  计划与步骤              /undo             回滚上一回合文件快照
/new /resume /rename  会话管理                /agents           子代理记录
/plugin               插件管理（reload/启停） /mcp              MCP 服务器状态与重连
/workflow             工作流切换与编辑         /skill            技能查看/重扫/生成
/rag                  RAG 状态 / 建索引 / 开关（插件）
```

## 项目结构

```
main.py               入口：TUI 主循环 / 无头模式 / AgentRunner 后台调度
core/                 核心模块
gui/workflow_editor.py  PySide6 工作流可视化编辑器（python -m gui.workflow_editor）
data/                 运行时数据
docs/                 文档
```

## 文档

- [插件开发指南](docs/plugin-development.md) —— `bawcode_sdk` 通用 SDK、hook 事件、宿主服务、SDK v1 迁移与测试
- [QQ 桥接](docs/qbridge.md) —— OneBot 对接与消息路由指北
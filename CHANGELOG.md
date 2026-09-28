# Changelog

## 2026-09-28 — 显示宽度改用 wcwidth 库，core/layout.py 移入回收站

- `core/layout.py` 迁入 `_recycle/layout.py`（回收站，保留备查不参与导入）；
- 宽度计算统一走 wcwidth（requirements 新增 `wcwidth>=0.8.0`）：
  - `core/ui.py`：`_char_width` 直接调用 `wcwidth`（控制字符 -1 记 1 列），
    `split_at_cells`/`_wrap_line`/`Layout`（增量折行缓存）并入 ui.py，逻辑不变；
  - `core/memory.py`：`_clip_title` 逐字符调用 `wcwidth`；
- 行为对比：仅 ZWJ（U+200D）与变体选择符（U+FE0F）由 1 列修正为 0 列
  （与 rich 渲染口径一致），其余字符宽度逐一比对无差异；
- `develop/test_input_layout.py` 改从 `core.ui` 导入，17 项全绿。

## 2026-09-26 — 历史会话自动保存 + /sessions 切换面板

### 存储层（core/session_store.py，新文件）

- 按项目隔离目录 `data/sessions/{project_id}/`（project_id 空回落 `_default`），
  文件名 `{YYYYMMDD-HHMMSS-hexx}.json`（时间可排序、Windows 文件名安全）；
- 原子写入：`.tmp` + `os.replace`（防 Ctrl+C 写一半损坏），utf-8 + ensure_ascii=False；
- `list_sessions` 扫描目录按 updated_at 倒序（损坏文件跳过不阻塞面板），
  `load_session_data` / `delete_session_data`；不做索引文件，磁盘即事实来源。

### 会话状态（core/memory.py）

- `Memory` 增 `session_id/session_created_at/session_title`，目录经
  `memory.sessions_dir` 配置（默认 `data/sessions`）；
- `save_session()`：空会话不写盘；标题派生自第一条 `type=="task"` 用户输入
  （按显示宽度截 30 列，中文占 2 列）；messages/plan/steps 原样落盘，
  恢复后与 `build_messages`/`_api_session_item` 过滤逻辑天然兼容；
  OSError 只记日志不上抛（自动保存不拖垮主循环）；
- `switch_to(data)` 原地替换状态（main/ui 持有同一实例）；`start_new_session()`
  旋转 ID 并清空。

### UI（core/ui.py）

- 照设置页模态范式新增历史会话面板：`sessions_mode` 标志 + `_compose_plain`
  渲染分支 + `show_sessions_form` 独立按键循环（不进 keymap/keyinput）；
- ↑↓ 选择 · Enter 切换/新建 · d 或 Del 删除（二次确认，当前会话与"＋ 新建会话"
  行不可删）· Esc/Ctrl+C 取消；视口滚动与选中反白复用设置页算法。

### 主循环（main.py）

- 新增 `/sessions` 命令（"＋ 新建会话" 置顶 + 磁盘列表）；
- `/clear` 改为"保存并开启新会话"（旧会话可随时 /sessions 切回）；
- 自动保存：每轮命令/对话结束 + `/exit` + Ctrl+C 均写盘。

### 测试

- `develop/test_session_store.py`：空会话不写盘、round-trip 保真
  （tool_calls/tool 消息）、switch_to 原地替换、标题截断、删除生效——19 项全绿；
- 真实运行（Windows Terminal + cmd）：/help 触发自动落盘 → /sessions 面板渲染 →
  /clear 开新会话 → 切回恢复消息 → d×2 删除（当前会话正确拒绝）→ /exit 干净退出。

## 2026-09-25 — 滚动条滑块鼠标拖拽 + 轨道点击翻页

### 事件层（core/keyinput.py）

- `_mouse_record_event(button_state, event_flags, x, y)` 扩展：左键按下/抬起 →
  `mouse_down/mouse_up`，移动 → `mouse_move`（坐标 0-based "x,y"），
  双击与右/中键忽略，滚轮行为不变；
- SGR 字节路径 `_mouse_sgr` 同步语义化（cb&32=移动、M=按下、m=抬起，
  坐标 1-based→0-based 归一）；
- `supported_kinds` 增加 mouse_down/mouse_move/mouse_up。

### UI 层（core/ui.py）

- `_compose_plain` 滚动条分支每帧向 `_row_meta` 写几何快照
  （scrollbar_on/x/body_top/scroll_total/thumb_h/thumb_top）；
- 滑块几何抽为模块级 `_scrollbar_geometry(total, tree_h, scroll)`（渲染与拖拽共用）；
- `_dispatch_key` 顶部路由 `mouse_*` → `_on_mouse`（不进键表、不触发键位重编译）；
- `_on_mouse` 拖拽状态机：down 命中滑块记录 grab_offset 进入拖拽，命中轨道
  上/下段翻页 ±tree_h；move 按滑块几何反算 scroll（`_set_scroll` 共用：
  光标同步夹进视口、到底恢复贴底）；up 结束；列外/设置页/Logo 占位忽略；
- 坐标 y 与 compose 行号按 1:1 映射，`_MOUSE_Y_OFFSET` 常量预留校准；
- `_scroll_tree_by` 收敛为 `_set_scroll` 的薄包装。

### 测试

- 记录/SGR 新用例（按下/抬起/移动/双击忽略/右键忽略/坐标归一）；
- 拖拽模拟：快照注入 → 抓滑块连续 move 单调滚动 → up 后 move 失效 →
  渲染后滑块跟随 → 轨道翻页不进拖拽态 → 列外点击无副作用；
- 全部套件通过（test_mouse_wheel 40 断言）。

## 2026-09-25 — 滚动延迟修复 + 会话区滚动条（替代 ↑N/↓N 行数）

### 滚动延迟（真机反馈：快滚明显滞后于手）

- 根因：`read_line` 每帧只分发 1 个事件（20fps → 20 事件/秒上限），WT 滚轮
  可达 ~25 事件/秒（探针实测），积压线性增长；`_scroll_tree_by` 每事件
  各自 render 又放大开销。
- 修复：
  - `read_line` 每帧分发本批**全部**事件（遇 submit/interrupt 返回即退出），
    顺带消除长按退格/方向键的同类回放滞后；
  - `_scroll_tree_by` 不再逐事件渲染，帧尾统一渲染（一帧 N 个滚轮事件只画一帧）。

### 会话区滚动条

- 取消原「↑N / ↓N」行数提示，改为右侧 1 列滚动条（用户选定样式）：
  `█` 滑块（accent）+ `│` 轨道（dim）+ `▲▼` 端点箭头（dim）；
- 会话区内容列宽 w→w-1 固定预留，内容不足一屏时该列留空（宽度不跳变）；
- 滑块长度 `max(1, round(tree_h²/total))`，位置随 scroll 比例映射；
- 树行按显示宽度裁剪/pad，宽字符不劈裂。

### 测试

- `test_mouse_wheel.py`：树焦点回归改为显式 render（分发不再自带渲染）；
  新增 compose 断言——超高内容出现 `█│▲▼` 且无 `[↑↓]数字`、不足一屏无滚动条；
- 全部套件通过。

## 2026-09-25 — 修复：会话区鼠标滚轮滚动在 WT/ConPTY 下不生效

### 取证（develop/probe_mouse_input.py，真实 WT 窗口）

- 仅开 `?1000h+?1006h`：8 秒滚动期间控制台输入队列 **0 条滚轮记录**——
  WT 未把滚轮转发给程序（被终端自己消费）；
- 补开 `?1002h`（按钮事件跟踪）后：滚轮以原生 `MOUSE_EVENT` 记录到达
  （每格 `delta=±128`，点击/移动同样到达）。

### 修复

| 层 | 改动 |
|----|------|
| `core/ui.py` enter/leave | 鼠标声明补 `?1002h`（对应 `?1002l` 关闭） |
| `core/keyinput.py` | INPUT_RECORD union 补 MOUSE_EVENT_RECORD；结构化读键路径解析滚轮记录 → `mouse_wheel` 事件（`_mouse_record_event`，移动/按钮忽略） |
| `core/ui.py` `_scroll_tree_by` | 滚动视口时把树光标同步夹进视口——否则树焦点下「光标行可见」夹紧把上滚立即弹回贴底 |

### 行为说明

- 鼠标捕获开启后，终端内文本选择改用 **Shift+拖拽**（WT 保留 Shift 给原生选择）。
- 点击/移动事件被忽略，不产生副作用；`BAW_MOUSE=0` 仍可整体关闭。

### 测试

- `test_mouse_wheel.py` 新增：记录解析（±128/±120、移动/按钮忽略）+ 树焦点滚动回归（上滚持续有效不回弹）
- 全部套件通过；pyflakes 零告警

## 2026-09-25 — 确认框收敛 / 设置页与宽度修复 / RAG 独立 / 死代码清理

### 修复

| # | 问题 | 修复 |
|---|------|------|
| #3 | 确认框 TUI 版（`_compose_confirm` + confirm_* 状态 + keymap DIALOG 上下文）从未参与交互——实际确认一直走 `_confirm_loop` 阻塞行输入 | 删除死路径；保留阻塞版为唯一实现；`main._handle_tool_confirm` 不再设置死状态 |
| #4 | `_compose_plain` 输入区宽度用硬编码 `"> "` 计算，自定义提示符（待完善> / 计划> 等）下折行与光标错位 | `inner_w` 改按 `_display_width(self._input_prompt)` |
| #7 | 设置页可退格删字符却无法输入（char 分支空操作） | 删除内联 backspace 编辑分支，文本编辑统一走 Enter 后行输入 |
| #8 | `_settings_apply` 模型段 `updates` 字典被系统段无条件覆盖 | 模型段改名 `model_updates`，返回值语义不再误导 |
| #10 | `execute_command`/`run_program` 以 utf-8 解码子进程输出，中文 Windows（cp936）下乱码 | 按控制台输出代码页（`GetConsoleOutputCP`）解码，非 win32 回落 utf-8 |
| #12 | 结构化读键路径丢弃 Ctrl 字母组合（uChar 为控制字符时返回 None） | 补 `_CTRL` 表映射为 hotkey |

### 重构

- **RAG 独立成 `core/rag.py`**：`RagStore`（内存文档 + 关键词兜底检索，外部接口挂点不变）；
  `memory.py` 的 `rag_add/rag_query` 变为委托。行为不变，后续向量库实现落在 rag.py。
- **死代码删除**（此前审查批准的批次）：
  - ui：`_compose_confirm`、`confirm_index/confirm_reject_edit/reject_buffer/reject_cursor`、
    `_schedule_paint/_flush_paint/_do_paint/render_partial_input`、`_append_text_burst`、
    `_read_key_windows/_read_key_posix`、`set_commands_source`、`_event_token/_binding_tokens/_is_binding/_action_hit/_should_submit/_should_newline`（测试改用 keymap 公开 API）、
    `_mode_key_label`、`isinstance(buffer, TextBuffer)` 双路径（buffer 恒为 TextBuffer）、
    `_cursor/_cursor_screen_row/_cursor_screen_col/_logo_done/_paint_at/_paint_pending/_last_frame_lines`、
    设置页防抖机制（`settings_debounce_*`/`_settings_text_dirty`/paint 防抖——随 #7 失去全部触发点）
  - keyinput：`_peek_enter_mod`、`_queue_depth`、`Decoder.chars`、`_buf`、`feed_printable_run`、`KeyEvent.__iter__`
  - textbuf：`move_doc_home/move_doc_end/to_list/word_end`；layout：`total_rows`
  - config：`update_settings/upsert_provider`、默认配置中 `settings_debounce_ms`；llm：`run_tool_calls`；commands：`_plugins_loaded`
  - 各文件未使用导入清理；`Context.DIALOG` 枚举移除（UI 无产生路径）

### 测试

- 全部 10 个套件通过（textbuf/keymap/keyinput_unit/key_binding/input_layout/mouse_wheel/keyinput_decoder/context_injection/paint_throttle/ime_bytes）
- `test_key_binding.py` 改为对 `core.keymap` 公开 API 断言；`test_paint_throttle.py` 移除兼容空壳场景
- 冒烟：h/l/中文端到端键入、自定义提示符渲染、确认路由、rag 读写、设置页组合渲染

## 2026-09-25 — 修复：单字符树绑定劫持输入框（h/l/` 无法打字）

### 问题

- `expand/collapse` 默认含单字符键 `l`/`h`，keymap 编译时被应用到全部上下文，
  输入框中键入 h/H/l/L 被解析为折叠/展开（光标移动甚至误翻树节点），
  实际打不出这两个字母；设置页可选的 `` ` ``/`~`（switch_mode）同理。
- HEAD（71d2649）旧分发以 `focus == "tree"` 为前提，重构编译表时丢失该门控。

### 修复

| 文件 | 要点 |
|------|------|
| `core/keymap.py` | 编译期单字符 token 不再写入 INPUT 上下文（树/对话框/设置保留） |
| `develop/test_keymap.py` | 回归：input h/l/H 插入、tree h/l 折叠展开、`` ` `` 输入插入 |

### 行为说明

- 单字符快捷键只在会话树（及对话框/设置页）生效；输入框内一律按字符插入。
- 多键绑定（ctrl+l、方向键等）不受影响。

## 2026-09-22 — 快捷键绑定真正接入 UI 分发

### 产品约定

- **Enter = 提交**（树焦点下 Enter 仍折叠节点）
- **Shift+Enter = 换行**（输入框插入 `\n`，不提交）
- Ctrl+Enter / 设置中的 `send` 额外键也可提交
- 设置页可配置的 `switch_mode` / `complete` / `switch_focus` / `scroll_*` / `expand` / `collapse` 由事件循环按 `self.keys` 匹配

### 修改

| 文件 | 要点 |
|------|------|
| `core/keyinput.py` | Windows `PeekConsoleInput` 识别 Shift+Enter → `newline`；CSI `13;2u` 兼容；`_pending` 预分类事件队列 |
| `core/ui.py` | `_event_token` / `_is_binding`；`read_line` 按绑定分发；`bind_config` 支持列表绑定；tips/设置提示更新 |
| `core/config.py` / `data/config.json` | 默认 `newline=shift+enter`、`switch_mode=shift+tab`；expand/collapse 为列表 |

### 测试

- `develop/test_key_binding.py`：绑定匹配与 Enter/Shift+Enter 语义
- `develop/test_keyinput_unit.py`：扫描码/CSI 回归

### 手工验证

1. 主界面：Enter 提交；Shift+Enter 换行；Ctrl+Enter 仍可提交
2. `/settings → 快捷键`：将发送改为 f5、切换模式改为 f2 后回主界面验证
3. 中文 IME 上屏后立刻方向键 / Shift+Enter
4. 若终端（ConPTY）未投递 Shift 修饰，Shift+Enter 可能回退为提交——以设置页可改键兜底

## 2026-09-20 (六) — 回退：keyinput 恢复为 getch + DBCS 重组层版本

### 背景

TMP 风格重写（逐字节即时产出事件、无中间缓冲）经用户实测**未能解决已知问题
且引入更多问题**（不合并字符导致事件量翻倍、0xE0 歧义吞字、无粘贴启发式导致
多行粘贴误提交、半截 CSI 丢失等）。执行回退。

### 恢复内容（core/keyinput.py，按 2026-09-19(五) 版本重建）

- getch 字节读取 + `_pend` DBCS 重组层（批次末按 console CP 统一解码，
  中文以单 char 事件产出）
- 扩展键前缀分支：孤立前缀零等待丢弃（IME 握手）；0xE0+非扫描码回退为字符
- CSI 半包保留 + HALF_TIMEOUT 超时决断；`_pend_timeout_check` 超时落地
- 粘贴管线完整恢复：bracketed-paste 收流 + conhost 启发式 + Ctrl+C 逃生
- 保留帧循环所需的 timeout<=0 真非阻塞语义

### 验证

- ime 单测 22/22（期望恢复：合并字符、E0 回退、启发式、超时落地）
- 帧断言 6/6、导入正常

### 教训记录

TMP 的简单读键在其场景（无粘贴需求、无 ConPTY、单字节搜索框）足够；
BAWCode 的 CSI/粘贴/DBCS 重组均有真实场景依据，不可为对齐而删。

## 2026-09-20 (五) — keyinput 重写为 TMP 风格自研读键（835 行 → 533 行）

### 用户要求

读键处理逻辑与 terminal-music-player 完全同构：**每次读一个字节即时产出事件**，
无 _buf/_pend 中间缓冲层、无批次末统一解码、无半包超时决断。

### 新架构（core/keyinput.py 重写）

```
getch 读一个字节 → 即时分类：
  0x00/0xE0 前缀  → 立即查队列配对扫描码 → 方向键/功能键；
                    孤立前缀（IME 握手）零等待丢弃
  0x1B           → CSI 解析（ConPTY 方向键/粘贴标记必需；10ms 内无续=单独 Esc）
  0x0D/08/7F/03/15/09 → submit/backspace/interrupt/clear/tab
  0x80-0xFF      → 多字节中文：即时续读第二字节（TMP _read_mb_char 同款），
                   按 console CP（GetConsoleCP，非 locale——避免 utf8_mode 干扰）
                   解码；UTF-8 三字节场景续读第三字节
  ASCII          → 直接 char 事件（不合并，TMP 行为）
```

删除：_buf 状态机、_pend 重组缓冲、_parse_win_buffer/_parse_csi_win 大状态机、
批次末统一解码、半包超时决断、粘贴启发式、read_events 批处理队列逻辑。
保留：日志设施、粘贴收流状态机（bracketed-paste 标记可达的终端）、Unix 路径。

### 行为对齐说明（与 TMP 一致的取舍）

- 连续打字产出多个独立 char 事件（TMP 不合并）
- 0xE0+非扫描码、孤立多字节首字节、半截 CSI → 丢弃（TMP 行为）
- **粘贴限制**：ConPTY/conhost 不透传 ESC[200~/201~ 标记（实测被 conhost 层
  消费），多行粘贴中部的回车会触发提交——TMP 同样如此，属平台限制；
  bracketed-paste 状态机保留（标记可达的终端下生效）

### 验证

- ime 单测 22/22（期望按 TMP 行为修正：不合并字符、E0/半截/孤立字节=丢弃）
- 帧循环断言 6/6、导入/编译正常、ConPTY harness 完成

## 2026-09-20 (四) — rich Live 接管渲染 + 固定 20fps 帧循环（完整对齐 music player）

### 架构

| 职责 | 改后 |
|---|---|
| 渲染输出 | rich Live(screen=False, auto_refresh=False) + Text.from_ansi 桥接（TMP 同款） |
| 主循环 | read_line 固定 20fps 帧循环：非阻塞读事件 → 逐事件分发 → 帧尾无条件渲染 → sleep 补足 50ms |
| 键输入 | keyinput 自研（rich 无 raw 输入能力；getch 字节路径，TMP 同款） |
| 文本状态 | buffer 字符列表（不变） |

删除：自研行 diff、partial 局部重绘、leading-edge 节流、空闲重画兜底、
光标钉位——帧节奏天然覆盖其全部职责（TMP 无输入卡顿的结构原因）。

### 兼容层

- `_schedule_paint/_flush_paint/_do_paint/render_partial_input` 保留为兼容
  入口（空操作或转 render），30+ 调用点零改动
- `_confirm_loop`（阻塞 input()）读取前停 Live、finally 重启
- 设置页循环走 render() 自动兼容

### 顺带修复

- **main.py 尾部存在一整段重复的"初始化+事件循环"代码块**（复制粘贴遗留）：
  /exit 退出第一层循环后会静默进入第二层循环继续运行。已删除。
- keyinput `_read_windows`：timeout<=0 时零等待返回 TICK（帧循环供拍需要；
  原实现 0 会退化为 20ms sleep）

### 验证

- 导入/编译正常；ime 单测 23/23（FakeConsole 重写为 getch 字节语义，按真实
  控制台行为注入）；帧循环断言 6/6；ConPTY harness 全 6 会话完成、粘贴无误提交
- ConPTY harness 下 rich Live 帧内容不进输出管道（stdout 为非终端管道，rich
  正确地不输出 VT）——属 harness 环境局限，真实终端 isatty=True 正常
  （TMP 同用法在用户环境长期正常）
- **待用户真实终端终验**


## 2026-09-20 (三) — 根因终审：终端延迟渲染帧 + 缺恒定重画兜底

### 定位方法与结论

用 100 行最小复现器（develop/minimal_repro.py）在用户环境二分：
full（BAWCode 式）卡 / tmplayer（TMP 同构）不卡 / getch、relative、
nochcur、nopaste 四个单变量模式全部仍卡 → 唯一剩余差异即元凶：

**TMP 的主循环无键也每 50ms 恒定重画整帧**。终端（conhost 与 WT 均有）
会偶发延迟渲染一帧应用输出——TMP 下一帧无条件重画自动修正，用户无感；
BAWCode 事件驱动，无键即无重画，被延迟的那帧（删除后的更新）滞留到
下一个键才显示，即"删除后字符不消失、打下一个字才刷新"。

此前多轮"应用 1ms 内正确写出"的日志结论与此不矛盾：应用确实写了，
是终端没有立即把它画上屏——只有持续重画能兜住。

### 修改（core/ui.py）

`read_line` 主循环空轮分支加**空闲重画兜底**：距上次绘制 ≥50ms
（`_IDLE_REPAINT`，对齐 TMP 20fps）时无条件重画一帧。
全帧成本 0.4ms，稳态 CPU 增量 <1%。

### 验证

- 单测 25/25、throttle 5/5、import 正常
- 空闲重画实测 ~17 帧/秒
- 待用户在双击路径（WT+搜狗）验证删除即时消失


## 2026-09-20 (二) — 完整采用 music player 渲染方案：每帧无条件全量重画

### 背景

上一轮只采用了 TMP 的"主屏模式"，保留了 BAWCode 自研的行 diff + partial
局部重绘；用户实测 conhost 下仍有残留（树区孤立字符残影）。重新审视 TMP
方案的本质：**没有 diff、没有局部重绘，每帧无条件重画整个界面**——结构上
不存在残留与失同步的可能。BAWCode 的 diff/partial 在实测 <1ms 的绘制成本
面前是负资产。

### 修改（core/ui.py）

1. `render()`：删除"与上次帧相同行跳过"的 diff，每帧写全部行
2. `render_partial_input()`：局部重绘逻辑删除，退化为兼容入口（= 全帧）
3. `_do_paint/_schedule_paint/_flush_paint`：partial 概念移除，统一全帧；
   leading-edge 节流保留（30ms 合并窗口，防粘贴级高频刷屏）

### 验证

- throttle 时序 6/5 场景全过（含新断言"每帧重画全部 30 行"）
- IME 单测 25/25、import 正常
- 全帧性能：200 条消息 + 30 行 ANSI 写出 = **0.41 ms/帧**（30fps 下 CPU ~1%）

### 用户验证路径

双击 py 启动（WT + 搜狗 + 真实键盘）：中文输入/删除应无残影、无延迟。


## 2026-09-20 — 根因锁定并修复：备用屏导致 WT 下 TSF IME 删除残影

### 根因（对照 terminal-music-player 实证）

用户路径（双击 py → Windows Terminal + 搜狗输入法 + 真实键盘）复现"删除后
残影直到下一键"；同程序 conhost 窗口正常；TMP 同环境完全正常。逐项对照两项目：

- TMP 用 rich `Live(screen=False)`——**从不进入备用屏**（rich 源码确认
  `_screen=False` 时不调用 set_alt_screen），全屏观感靠主屏每帧相对重画
- BAWCode `enter()` 写 `[?1049h` 切**备用屏** + 绝对定位行 diff

Windows Terminal 的 TSF 输入层（搜狗等现代 IME 的集成路径）在备用屏下
组合上下文与行级更新失同步：应用删除字符后 1ms 内已写入新行（日志实证），
但 TSF 组合层缓存遮蔽旧画面，直到下一次按键事件触发重同步——即
"删除不消失、打下一个字才刷新"。英文不经 TSF；conhost 走 IMM32 不经 TSF；
注入按键绕过 IME——四组观测全部吻合。

### 修改（core/ui.py）

- `enter()/leave()` 默认改**主屏模式**（对齐 TMP）：`?25l + 2J + H + 2004h`，
  退出清屏回主屏；代价是 shell 提示符被覆盖（回车出新提示符）
- 保留 `BAWCODE_ALT=1` 环境变量回退备用屏旧模式
- （上一轮已加）每次绘制后物理光标定位到输入光标处（IME 组合窗锚定）

### 验证

- 单测 25/25、throttle 5/5、import 正常
- conhost 实例实跑主屏模式：输入/删除链路正常
- **待用户在 WT 双击路径验证**（唯一无法注入复现的路径）


## 2026-09-19 (五) — 输入管线重构 P0+P1（对齐 qwen-code 设计，见 develop/qwen-code 分析报告）

### P0-1 超时出口（逃生舱优先）

- CSI / 0xE0 半包滞留超 200ms（HALF_TIMEOUT）强制决断：ESC 前缀按 Esc、0xE0 按字符落地、0x00 丢弃——防终端丢字节导致输入假死
- DBCS 重组缓冲 `_pend` 加 8 字节上限 + 100ms 超时 latin-1 落地
- 粘贴收流中 Ctrl+C 逃生舱优先：截断粘贴、flush 已收内容、交出 interrupt
- `flush_input` 同步清理全部新状态

### P0-2 粘贴管线（此前粘贴多行文本的每行 
 会误触发提交）

- `enter`/`leave` 写 `ESC[?2004h/l` 开关 bracketed-paste
- 解码层支持 `ESC[200~`/`ESC[201~`：粘贴期间字节整体收流，合成单个 `("paste", text)` 事件；end 丢失 1000ms 空闲超时强制 flush
- conhost 无粘贴标记 → 启发式：同批次多字符且回车在批次**中部**（或含 Tab）→ 合成 paste；回车在末尾视为正常提交（IME 上屏+回车常同批，不可误判）
- `read_line` 新增 paste 分支：整体插入 buffer，换行归一，绝不触发 submit
- 已知残留：conhost 下单行粘贴且末尾带回车仍会提交（该终端无标记可区分）

### P0-3 按键路径成本降为 O(输入区)

- `_tree_rows` 行内容缓存（key=宽度/会话签名/折叠态/焦点/光标）
- 输入区 wrap 与 cursor_row 缓存
- 实测 `_compose_plain`：200 条消息 12.1ms → 0.21ms；按键路径（树缓存命中）0.09ms

### P1 事件批处理

- keyinput 新增 `read_events()`：一次抽干输入队列返回事件列表
- `read_line` 改为队列连续消费（绘制频率由 leading-edge 节流天然合并，不随事件数增长）

### 验证

- 单测 25/25：原 17 用例回归 + 粘贴 5 用例（bracketed/conhost/不误判/Ctrl+C 逃生）+ 超时 2 用例
- throttle 时序 5/5 回归
- 真实 ConPTY 运行：多行粘贴合成单个 paste 事件且无误提交；半截 CSI 由 ConPTY 系统层消化（应用层超时出口为纵深防御，单测覆盖）
- 全部模块导入正常


## 2026-09-19 (五) — 新增分级日志系统：debug/info/warn/error，粒度入 config.json

### 背景

程序此前几乎没有运行日志（仅 keyinput 有 BAW_LOG_KEYINPUT 独立诊断开关），
LLM 调用失败、插件加载异常、allowlist 损坏等关键路径失败均被静默吞掉，
问题只能靠复现定位。需要一个不依赖第三方库、不影响 TUI 渲染的分级日志。

### 新增（core/log.py）

- 四级日志 **debug / info / warn / error**（另有 off 关闭），线程安全，
  写入 `data/log/bawcode-YYYYMMDD.log` 按天分文件，旧文件按保留天数自动清理；
- 单行格式 `时间 [级别] [模块] 消息`，超长内容截断 4000 字符、换行压平，便于 grep；
- %-style 惰性格式化：被过滤的日志不构造字符串；
- `Config` 初始化时以 config.json 的 `log` 段调 `log.init()`；模块导入时先
  bootstrap 读一次默认配置，保证配置加载本身的日志也受粒度控制。

### 配置（config.json → "log" 段）

```json
"log": {
  "level": "info",       // 全局阈值 debug/info/warn/error/off
  "modules": {},         // 按模块覆盖，如 {"llm": "debug", "ui": "off"}
  "console": false,      // 同步输出到 stderr（默认仅文件）
  "days_to_keep": 14     // 保留天数，0 永久
}
```

模块名与 core 下文件同名：main/config/llm/memory/tools/register/commands/
hooks/policy/ui。

### 接入点

- **llm**：请求/响应（model、字数、tool_calls 数、耗时）、逐次重试告警、
  最终失败、余额查询、策略判定、工具执行错误（原先仅吞成返回字符串）；
- **memory**：会话初始化、长期记忆读写（JSON 损坏有 error）、压缩触发、
  计划/步骤/事实写入、RAG；
- **tools**：命令与程序执行（命令行、退出码、超时）、文件读写编辑；
- **commands**：命令执行、未知命令、插件加载失败（原先 `except: continue` 静默）；
- **config**：配置加载/保存/解析失败、模型切换、提供商保存；
- **policy/hooks/ui/main**：allowlist 损坏、外部接口回落、主题回落、
  TUI 进出、任务开始/完成/轮次上限。ui 仅挂低频点，渲染热路径零日志。

### 验证

- 自测 `develop/test_log.py` **12/12**：级别过滤、模块粒度覆盖（llm=debug/
  ui=off）、全局 off、惰性格式化、长文本截断保单行、过期清理、当日保留。
- 真实运行链路：Config 载入 data/config.json → memory/工具真实执行 →
  `data/log/bawcode-20260919.log` 落盘，默认 info 下 debug 被过滤；
  临时配置 modules={"llm":"debug","ui":"off"} 全链路生效，非法级别值
  回退全局阈值；console=true 时 stderr 同步输出。
- 回归：`develop/test_paint_throttle.py` 5/5；`import main` 正常。

## 2026-09-19 (四) — 删除仍卡顿：trailing 节流改 leading-edge（每键立即刷）

### 背景

上轮 throttle 修复了"冻结"（不再无限推迟刷新），但保留 trailing 延迟：
每次删除都要等满 50ms 节流窗口，叠加主循环 20ms 检查间隔，单键删除仍有
50-70ms 上屏延迟——这是剩余卡顿感的来源。music player 是 leading-edge：
按键处理完当帧立即画。

### 修改（core/ui.py）

`_schedule_paint` 改为 **leading-edge 节流**：

- 距上次绘制 ≥ 30ms（`_PAINT_MIN_INTERVAL`，≈33fps）且无挂起 → **立即绘制**
  （画前先同步候选区，`_refresh_candidates` 有未变跳过）；
- 间隔内（粘贴级高频）→ 挂起，由主循环 tick 到点补刷，保证最后一次更新不丢；
- 新增 `_do_paint` 统一绘制动作与 `_last_paint` 时间戳；partial/全帧合并、
  input_h 变化退化全帧逻辑保持。

### 验证

- 时序仿真 `develop/test_paint_throttle.py` 5/5：单键删除立即刷（0 延迟）；
  30Hz 连按删除 10/10 每键即画；高频合并 + 补刷不丢；全帧升级；input_h 退化。
- **真实运行**（ConPTY 注入连打+连删）：删除事件 → 渲染延迟平均 **0.1ms**、
  最大 1ms（修复前 trailing 50-70ms+）；连续删除渲染间隔 42-66ms。
- 输入单测 17/17 回归通过。


## 2026-09-19 (三) — 输入/删除卡顿：绘制调度 debounce 冻结修复（对齐 music player）

### 背景

输入后有短暂卡顿、连按删除键卡顿严重。对照 terminal-music-player（其输入框
无此问题）：它是固定 20fps 主循环**每帧无条件重绘**，防抖只用于昂贵的下游
计算（列表过滤），显示零延迟。

### 根因（两层，均在 core/ui.py 绘制调度）

1. **`_schedule_paint` 是 debounce（防抖重置）**：每次按键重置 50ms 定时器。
   键盘重复率约 30Hz（33ms 间隔 < 50ms），连续键入/连按删除时定时器永远被
   重置、永远到不了刷新点——界面冻结到停手才一次性上屏。删除按住不放最严重。
2. **最频繁的操作走了最贵的路径**：backspace/删除/光标移动/英文字符调度的是
   全帧 `render()`（`_compose_plain` 全帧组合 + 30 行 diff），只有中文输入走
   partial 局部重绘——方向反了。

### 修改（core/ui.py）

1. `_schedule_paint`：debounce → **throttle**——pending 已挂起时不顺延定时器，
   连续键入每 50ms 必刷一次（与 music player 20fps 同级）；参数 `cjk` 改为
   `partial`（默认 True），输入区操作统一局部重绘。
2. `_flush_paint`：消费后 `_paint_partial` 恢复默认 True，避免 False 残留把
   后续 partial 调度降级为全帧（时序仿真发现）。
3. `render_partial_input`：输入区高度变化（候选区出现/消失、树底行重排）时
   退化全帧，修复树底行残留。

### 验证

- 时序仿真 `develop/test_paint_throttle.py` 5/5：30Hz 连按删除期间渲染 ≥5 次
  （修复前 0）；单键 ≤80ms 上屏；throttle 不重置；partial/全帧合并正确；
  input_h 变化退化全帧。
- 真实运行 `develop/conpty_input_test.py` 会话 4：30Hz 连打 10 字 + 连按
  15 次删除（830ms）期间渲染 10 次 partial（间隔 80-110ms），不再冻结。
- 输入单测 `develop/test_ime_bytes.py` 17/17 回归通过。


## 2026-09-19 (二) — 真实运行取证：conhost DBCS 逐字节投递才是乱码根因

### 背景

上一轮把 getch 换成 getwch 后用户反馈问题未解决。本轮加入输入诊断日志
（`BAW_LOG_KEYINPUT=1` → `develop/keyinput.log`），并用 ConPTY / conhost
双 harness 真实运行 `python main.py` 注入输入取证，推翻了上一轮结论。

### 真实运行证据（develop/conpty_input_test.py / conhost_dbcs_test.py）

- **ConPTY（Windows Terminal 路径）**：conhost 把 UTF-8 输入正确翻译成整字
  KEY_EVENT，getwch 直接拿到 '你'(U+4F60)，本来就没问题。
- **传统 conhost（中文 Windows，console CP=936）**：IME 上屏文本被按控制台
  代码页**拆成逐字节 KEY_EVENT 投递**，UnicodeChar=原始字节值。getwch 读到的
  是 U+00C4/U+00E3…伪宽字符序列——乱码在应用读取之前就已注定，**换 getwch
  无法解决**。实测注入 GBK 逐字节记录：日志显示 `raw unit 'Ä' U+00C4 …` →
  `event char 'ÄãºÃÖÐÎÄ'`，与用户乱码一致。

### 修改

| 文件 | 变更 |
|------|------|
| `core/keyinput.py` | ① conhost DBCS 字节重组层：U+0080..U+00FF 伪宽字符累积为字节，**批次结束（kbhit 耗尽）统一解码**（console CP 优先，utf-8/gbk 兜底）——中途解码会让 GBK 在 UTF-8 多字节中途抢跑解出乱字；② 孤立 0xE0 字节视为扩展键前缀交解析层配对（ConPTY 方向键形态 à+扫描码）；③ 输入诊断日志设施（env 开关） |
| `core/llm.py` | `chat()` 重试捕获扩为 `Exception`：openai NotFoundError 等 API 错误此前不在捕获元组内，任何提交都会崩溃退出整个 TUI（真实运行发现：提交"你好"后程序死亡，极大放大"问题未解决"体感） |
| `develop/` | `test_ime_bytes.py` 三形态 17 用例；`conpty_input_test.py` ConPTY 真实注入；`conhost_dbcs_test.py` conhost 逐字节注入 |

### 真实运行验证结果

- conhost DBCS 逐字节 GBK "你好中文" → 日志 `dbcs decoded '你好中文'` → `event char '你好中文'` ✓
- ConPTY 整字直通、à+扫描码方向键、CSI、Shift+Tab、回车/退格回归 ✓
- 单元 17/17（整字/DBCS GBK/DBCS UTF-8/跨批次/首字节 0xE0 汉字/功能键）✓
- LLM 404 → `error` 字段返回 UI 显示，不再崩溃 ✓

### 遗留说明

- ConPTY 收到 GBK 原始字节会得到 U+0132 类乱字（conhost 输入状态机按错误
  编码翻译，且不在伪宽字节区间无法重组）——真实 WT 不会发 GBK，属人为对照。
- 建议真实终端手测：cmd/传统控制台 IME 输入中文（本修复主场景）。


## 2026-09-19 — 中文输入乱码/吞字修复 + 输入绘制卡顿治理

### 背景

1. **乱码**：输入框输入中文后显示为 latin-1 mojibake（如"你好"→"ä½ "），部分汉字（GBK/扫描码首字节撞 `0xE0`）被整体吞掉。根因：`keyinput._fill_win` 实际使用 `msvcrt.getch()` 逐字节读取并 `chr(code)` 按 latin-1 入缓冲，IME 上屏的多字节中文被拆散，且 UI 层无任何重组逻辑（与模块 docstring 声称的 getwch 路径不符）。
2. **卡顿**：中文输入时每字上屏存在 200ms 硬延迟（`_schedule_paint(cjk=True)`），且每次刷新（含 partial）都经 `_compose_plain` 全帧重算——`_tree_rows → _flatten_tree → _build_tree` 每帧全量重建会话树，单次开销随消息数线性增长（实测 200 条消息 12.1ms）。

### 修改文件

| 文件 | 变更要点 |
|------|----------|
| `core/keyinput.py` | Windows 读字符改 `getwch()` 宽字符；代理对合并；`0xE0` 前缀回退防吞字；`flush_input` 同步改宽字符 |
| `core/ui.py` | 中文绘制延迟 0.2s→0.05s；`_flatten_tree` 增加签名缓存；`_refresh_candidates` 未变化跳过；输入框续行前缀按显示宽度补齐 |
| `develop/test_ime_bytes.py` | 重写为 getwch 字符流全链路验证（13 个用例） |

### core/keyinput.py

1. **`_fill_win` 改用 `msvcrt.getwch()`**
   - IME 中文整字上屏（UTF-16 码元），不再经过控制台代码页字节流，UTF-8/GBK 差异彻底规避
   - BMP 外字符（emoji）分两次返回高低代理码元，立即续读低代理并解码为单码点；孤立高代理保留单码元不拼错字
   - 扩展键仍为 `0xE0/0x00` + 扫描码，扫描码/CSI 解析路径不变
2. **`_parse_win_buffer`：`0xE0` 前缀回退**
   - 前缀后下一字符不是已知扫描码（且前缀为 `0xE0`）时按普通字符（à）处理并保留后续字符，不再整对丢弃；`0x00` 前缀维持无条件消费
3. **`flush_input` 同步改 `getwch`**，清队列与读队列同语义
4. **模块 docstring 更新**，与实现一致并说明回退策略

### core/ui.py

1. **`_schedule_paint`：中文/英文统一 50ms 合并绘制**
   - IME 争用已由 `render_partial_input`（只写输入/状态行）化解，去掉 0.2s 兜底延迟，消除每字上屏滞空感
2. **`_flatten_tree` 签名缓存**
   - 新增 `_tree_signature()`（消息条数/总长度、task、steps 数、plan 状态等），命中时复用 `_build_tree` 结果，展平 walk 仍每次执行（折叠实时生效）
   - `refresh_from_session` 显式置缓存失效双保险
   - 实测 `_compose_plain` 单次 12.1ms → 4.96ms（200 条消息场景）
3. **`_refresh_candidates` 未变化跳过**：记录上次 (buffer, cursor, config)，20ms 轮询下重复调用近乎零开销
4. **输入框续行前缀 `" " * len(prompt)` → `" " * _display_width(prompt)`**，修正含中文 prompt（如"待完善> "）多行输入错位

### 预期效果

- Windows Terminal / conhost 下中文 IME 输入、整句粘贴均正常显示，不再乱码、不再吞字
- 每字上屏延迟 200ms → 50ms；长会话下输入刷新开销降为原来的约 40%，且不再随会话增长恶化树重建部分
- 方向键、Shift+Tab、Esc、回车、补全行为与修复前一致（回归用例覆盖）

### 手工验证建议

1. Windows Terminal：IME 逐字输入与整句粘贴中文，确认无乱码/无缺字（重点试"唰、唳、喃"等字）
2. 输入框中文连续输入流畅度；`/mo` Tab 补全、↑↓ 历史、Backspace
3. `/settings` 各 Tab 的 text 字段输入中文；Shift+Tab 切模式
4. 长会话（100+ 条消息）连续打字观察流畅度

## 2026-02-17 — 键盘输入可靠性（对齐 terminal-music-player）

### 背景

BAWCode 在中文输入后、以及设置界面等场景出现「部分按键失效」，尤以 **↑↓←→** 为重：
方向键有时被解析成 `escape`（退出设置）、有时变成 `hotkey`（设置页不处理）、
有时被 UI 层二次读键吞掉扫描码前缀。

对照实现：`terminal-music-player` 的 `mp/keyinput.py` 在 Windows 上固定使用
**msvcrt 扫描码路径**（`0xE0/0x00` + scan），**不开启** `ENABLE_VIRTUAL_TERMINAL_INPUT`，
每帧只读一个逻辑键，因此方向键与 IME 冲突面更小、行为稳定。

### 修改文件

| 文件 | 变更要点 |
|------|----------|
| `core/keyinput.py` | Windows 读键与 CSI 半包策略重写；新增 `direction_of()` |
| `core/ui.py` | 关闭输入 VT；设置页/输入框兼容方向事件；去掉二次 drain；choice 空字段防护 |

### core/keyinput.py

1. **Windows 扩展键对齐 music player**
   - 读键统一走 `msvcrt.getch()` 原始字节入 `_buf`，解析 `0xE0/0x00` + 扫描码表 `_WIN_SCAN`
   - ↑↓←→ / Shift+Tab（scan 15）不依赖终端是否投递 CSI
   - 扩展键半包（仅有前缀、第二字节未到）**保留缓冲返回 `tick`**，禁止 `pop` 丢弃导致方向键失效

2. **CSI 半包不再升级为 `escape`**
   - `ESC [` / `ESC O` / `ESC [ 参数…` 未收齐时：**保留缓冲 + 返回 `tick`**，下一帧续读
   - 仅「确认单独 Esc」或「ESC + 无法形成序列的字节」才返回 `escape`
   - 设置页因此不再被拆包方向键误触发退出/未保存确认

3. **方向类 CSI 返回基础 kind**
   - 无修饰（或应用光标键）的 `ESC [ A/B/C/D` 等直接返回 `up/down/left/right/home/end`
   - 带 Ctrl/Shift/Alt 的序列返回 `hotkey`（值为 `ctrl+up` 等），由 UI 层兼容

4. **Unix 路径对齐 music player 语义**
   - 仅单独 Esc 才是 `escape`；CSI 半包超时返回 `tick`，避免方向键被误判

5. **新增 `keyinput.direction_of(kind, value)`**
   - 将 `kind=up` 与 `hotkey=ctrl+up` 统一归一成方向名，供设置页/输入框使用

### core/ui.py

1. **`_enable_windows_ansi`：只开输出 VT**
   - 去掉 `ENABLE_VIRTUAL_TERMINAL_INPUT (0x0020)`
   - 与 music player 一致：msvcrt 扫描码成为主路径；Shift+Tab 仍可用（scan 15 或 CSI Z）

2. **设置页 `show_settings_form`**
   - ↑↓←→ 经 `_key_direction()` → `keyinput.direction_of()` 识别
   - 兼容 `hotkey` 形式的方向事件，避免「事件到了但设置页吞键」

3. **输入框 `read_line`**
   - 同样改为 `_key_direction()` 处理方向，历史/树导航/候选列表更稳

4. **`_append_text_burst`：禁止 UI 层二次 drain**
   - 不再从 `msvcrt.kbhit()/getwch()` 抽队列
   - 原因：二次读取会吃掉 `0xE0/0x00` 前缀，使后续扫描码变成「普通字符」，方向键失效
   - 可打印合并已由 `keyinput` 完成，UI 只追加事件里的 `value`

5. **`_settings_cycle_choice` 空字段防护**
   - `settings_fields` 为空、索引越界、字段缺 `key` 时直接 return，避免 ←→ 触发 IndexError 导致设置循环崩溃

6. **多字符中文**
   - `char` 事件使用 `all(ord(ch) >= 32 for ch in value)`，避免对 IME 合并串调用 `ord(整串)` 抛 TypeError

### 预期效果

- 设置界面 ↑↓ 移动、←→ 切换 choice 在 Windows conhost / Windows Terminal / VS Code 下均可捕获
- 中文 IME 上屏后方向键不因拆包/二次 drain 失效
- 按方向键不会被误当成 Esc 退出设置页

### 手工验证建议

1. 进入 `/settings`，在提供商/模型/系统/快捷键各 Tab 连续 ↑↓←→
2. 在输入框输入中文后立刻按 ↑↓←→（历史/树/光标）
3. Enter 编辑 text 字段，Esc 取消后再按方向键
4. Windows Terminal 与传统 conhost 各测一遍；Shift+Tab 应仍能切换 mode

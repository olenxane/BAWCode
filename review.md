# BAWCode 输入管线审查指南（review.md）

> 面向代码审查者的完整索引：问题背景、排查历程、最终根因、涉及文件与行号、
> 审查重点、已知风险。审查对象：`core/keyinput.py`（输入解码）、`core/ui.py`
> （输入循环与渲染）、`core/llm.py`（一处异常兜底）。

---

## 1. 问题症状（用户实测）

1. **中文输入卡顿**：打字/删除后字符延迟上屏，"删除后字符不消失，打下一个字才刷新"；
2. 历史：中文上屏曾显示乱码（已修复）；英文输入始终流畅；
3. 环境：中文 Windows（ACP=936）、双击 py 启动（Windows Terminal 或 conhost）、
   搜狗/微软拼音均复现。terminal-music-player（下称 TMP）同环境完全正常。

## 2. 排查历程（八轮，结论多次被推翻——审查时注意注释里的历史包袱）

| 轮次 | 假设 | 结果 |
|---|---|---|
| 1 | getch 逐字节 + latin-1 转换 → 乱码 | ✔ 真：上屏乱码修复（getwch + 重组） |
| 2 | 绘制 debounce 冻结 | ✔ 真：改为 throttle |
| 3 | conhost DBCS 逐字节投递 | ✔ 真：加字节重组层（对 getwch 伪宽字符） |
| 4 | trailing 节流仍延迟 | ✔ 真：改 leading-edge |
| 5 | getwch 读错字段（探针证伪：两字段数据都对） | ✘ 假设不成立 |
| 6 | IME 组合期按键透传/光标缺失 | 部分成立（光标钉位保留） |
| 7 | 备用屏 TSF 失同步 | 部分成立（主屏模式保留） |
| 8 | **终端偶发延迟渲染帧 + IME 握手字节被半包等待挂起** | ✔ **终审根因**（minimal_repro.py 二分锁定） |

**最终修复**（2026-09-20）：读键换回 `getch` 字节路径（TMP 同款）+ 孤立
`\x00/\xe0` 前缀零等待丢弃 + 空闲重画兜底（50ms）。**此轮修复尚未经用户
最终验证**——审查时请特别确认这两处逻辑。

## 3. 最终根因（两条复合）

1. **IME 握手挂起**：IME 删除刚上屏字符时会先注入孤立 `\x00`（NUL）握手字节
   （日志实证：`\x00` 到达 → 应用等第二字节 → IME 自身超时约 1.3s 后才放行
   真正的 backspace）。旧代码对孤立前缀做 200ms 半包等待再丢弃，加剧挂起；
   `getwch`（宽字符读取）与 TMP 的 `getch`（字节读取）走控制台 A/W 两条
   互操作路径，对 IME 事件流的握手响应行为不同。
2. **终端延迟渲染帧**：conhost/WT 会偶发延迟渲染一帧应用输出（应用 write+flush
   毫秒级完成、内容正确，但终端不立即上屏）。TMP 靠"无键也每 50ms 恒定重画"
   在下一帧自动修正；事件驱动 TUI 缺这层兜底时表现为"删除不消失直到下一键"。

## 4. 文件与行号索引

### core/keyinput.py（输入解码层，835 行）★ 审查重点

| 行号 | 符号 | 职责与审查点 |
|---|---|---|
| 49-80 | `_log`/`_log_console_info` | 诊断日志（`BAW_LOG_KEYINPUT=1` → develop/keyinput.log）。记录每个 raw 字节、事件、绘制、IME 握手字节丢弃 |
| 83-99 | `_decode_encodings` | DBCS 重组的解码候选序：console CP 优先（GBK/65001），utf-8/gbk 兜底 |
| 202 | `KeyReader.__init__` | 状态字段：`_buf`（待解析字符）、`_pend`（DBCS 字节重组）、`_half_at`（CSI 半包时间戳）、`_pasting/_paste_chunks`（粘贴收流） |
| 241 | `flush_input` | 清空全部状态；读队列用 `getch`（必须与 `_fill_win` 同 API，否则队列错位）|
| 257-262 | `_decode_pend` | `_pend` 严格全量解码；失败返回 None |
| 268-284 | `_flush_pend_before` | 非伪字节入缓冲前冲刷 pending；孤立 0xE0 走扩展键特判 |
| 286-303 | `_pend_timeout_check` | DBCS 重组超时（100ms）/超限（8 字节）出口：latin-1 落地不丢字 |
| 305-337 | 粘贴状态机 | bracketed-paste（ESC[200~/201~）收流；Ctrl+C 逃生；conhost 启发式（回车在批次**中部**才判粘贴）|
| **340** | **`_fill_win`** | **本轮核心改动**：`getch` 字节读取（TMP 同款）。0x80-0xFF 字节进重组层；其余入 `_buf`。审查点：与 `flush_input` 的 API 一致性 |
| 390 | `_read_windows` | 主读取循环。含粘贴 idle 超时（1s）、conhost 粘贴启发式、半包续读 |
| 433 | `_parse_win_buffer` | 事件解析主状态机。**本轮核心改动**在扩展键前缀分支（约 445-460 行）：孤立 `\x00/\xe0` **零等待丢弃**（原为 200ms 半包等待——IME 握手挂起根因） |
| 532-540 | `_half_deadline_passed` | CSI 半包 200ms 超时决断（防丢字节假死，纵深防御） |
| 542+ | `_parse_csi_win` | CSI 序列解析：方向键/功能键/粘贴标记/修饰键 |
| 679 | `_read_unix` | Unix 路径（未在本轮问题范围） |
| 775 | `read_events` | P1 批处理 API：一次抽干队列返回事件列表 |

### core/ui.py（输入循环与渲染，2398 行）

| 行号 | 符号 | 职责与审查点 |
|---|---|---|
| 488/506 | `enter`/`leave` | 终端模式：默认**主屏**（TMP 对齐，去 `?1049h` 备用屏）；`BAWCODE_ALT=1` 回退旧模式；`?2004h` 粘贴协议 |
| 664-770 | `_tree_signature`/`_flatten_tree`/`_tree_rows` | 会话树签名缓存（避免每帧重建树） |
| 772 | `_compose_plain` | 全帧组合。含输入 wrap/光标行缓存（约 820-840）、物理光标屏幕坐标计算（约 910-925） |
| **992** | **`render`** | **TMP 方案核心**：每帧**无条件**重画全部行（无 diff、无 partial） |
| 1010 | `_write_cursor_pos` | 物理光标钉位到输入光标处（IME 组合窗锚定；qwen-code §6.2 方案） |
| 1028 | `render_partial_input` | 兼容入口 = 全帧（partial 已废弃，防残留） |
| 1035-1036 | `_PAINT_MIN_INTERVAL`/`_IDLE_REPAINT` | 节流 30ms / 空闲重画 50ms（TMP 兜底） |
| 1038 | `_do_paint` | 统一绘制动作（全帧 + 日志） |
| 1049/1069 | `_schedule_paint`/`_flush_paint` | leading-edge 节流：间隔外立即刷，间隔内挂起补刷 |
| **1145** | **`read_line`** | 输入主循环。**本轮核心改动**在空轮分支（约 1170-1177）：空闲 ≥50ms 无条件重画（终端延迟渲染帧的兜底）。另有 paste 事件分支（约 1280）：整段插入、不触发 submit |
| 2342 | `_read_events` | 批处理读取转发 |

### core/llm.py

| 行号 | 变更 | 说明 |
|---|---|---|
| 191 | `except Exception as e` | chat() 重试捕获扩为 Exception：openai NotFoundError 等曾穿透导致整个 TUI 崩溃（提交任何文本即死） |

### develop/（取证与测试工具）

| 文件 | 用途 |
|---|---|
| `test_ime_bytes.py` | 25 用例输入解码单测（FakeConsole mock msvcrt；**注意：mock 仍是 getwch 语义，本轮换 getch 后未适配，用例里的"伪宽字符"输入形态已不代表真实路径，待重构**）|
| `test_paint_throttle.py` | 绘制调度时序断言（6 场景） |
| `minimal_repro.py` | ★ 最小复现器（二分定位元凶的关键工具）：full/tmplayer/getch/relative/nochcur/nopaste 六模式 |
| `conpty_input_test.py` | ConPTY 真实注入 harness（6 会话场景） |
| `conhost_dbcs_test.py` | conhost WriteConsoleInputW 逐字节注入 |
| `test_real_ime_probe.py` | getch vs getwch 现场探针（用户跑过，证明两字段数据都对） |
| `qwen-code输入框与键盘输入实现分析报告.md` | qwen-code 架构分析（P0/P1 修改的依据） |

## 5. 已知风险与待办（审查请重点过目）

1. **最新修复（getch + 零等待放行 + 空闲重画）未经用户最终验证**——语法/导入
   已检查，但 `test_ime_bytes.py` 的 mock 未适配 getch 字节流（用例仍按
   getwch 伪宽字符注入），该测试当前**不代表真实路径**。重构方向：FakeConsole
   改为字节语义 + console CP 注入。
2. `flush_input` 与 `_fill_win` 必须始终使用同一 API（getch）——混用会导致
   控制台队列 A/W 记录错位（历史教训）。
3. 主屏模式副作用：退出后 shell 提示符被覆盖（按回车重现）；切换窗口后
   重绘依赖 idle repaint（50ms 内自愈）。
4. conhost 单行粘贴+末尾回车仍会提交（该终端无粘贴标记，无法区分；见
   keyinput.py 粘贴启发式注释）。
5. 孤立 `\xe0` 现在会被丢弃而非按字符处理（此前有"à 回退为字符"逻辑）——
   ConPTY 下 `à`+扫描码成对到达不受影响；单独 `à` 字符输入在 ConPTY 下
   会不会被误伤需要留意（getch 下 à = 0xE0 单字节，会走伪字节重组层而非
   前缀分支，理论上安全，但无测试覆盖）。
6. 空闲重画在设置页/确认框等界面是否需要（当前只在 read_line 主循环），
   其他界面（show_settings_form 用自己的循环）未加兜底。

## 6. 建议审查顺序

1. `keyinput._fill_win`（340）→ `_parse_win_buffer` 扩展键分支（433-460）：
   本轮两处核心改动，确认与 TMP 语义一致；
2. `ui.read_line` 空轮兜底（1170-1177）+ `render`（992）：恒定重画链路；
3. `test_ime_bytes.py` mock 与真实路径的偏差（风险 #1）；
4. 其余按第 4 节索引通读。

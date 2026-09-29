# Changelog

## 2026-09-29 — 技能加载系统：SKILL.md 双层目录 + load_skill 工具 + /skill 命令

- **渐进式披露三层**（core/skills.py）：启动仅扫 SKILL.md frontmatter 元数据（每条
  百 token 级）进 [可用技能] 清单，正文经 load_skill 工具按需读取并缓存，
  scripts/references 由模型经 read/run_command 自行取用；pyyaml 为新增依赖
  （requirements.txt），缺失时技能系统降级关闭并告警，不影响主流程。
- **双层目录**：全局 data/skills + 项目 {workspace}/.bawcode/skills（项目级同名覆盖
  全局，与 Agent.md/Projects 记忆范式一致）；格式兼容 SKILL.md+YAML frontmatter
  规范，附 data/skills/example 示例技能作为新技能模板。
- **注入通道**（memory.build_context_supplements）：[可用技能] 清单为独立 system
  补充，每回合重建、不进会话历史——不参与回合末剥离与阈值压缩，常驻成本仅清单
  预算（skills.metadata_budget_tokens，超限先降级为仅名称再截断）。
- **load_skill 工具**（core/tools.py）：只读（policy.SAFE_TOOLS 放行 manual 模式）；
  行内限额沿用 context.large_tools（已列入，32k），超限部分由 add_tool_result 统一
  外置 toolstore 附回读指针；不加入 tool_whitelist——技能内容生命周期=回合内，
  需要时模型重读（幂等、纯磁盘读）。技能脚本不新开执行通道，正文引导经 run_command
  走现有 policy 确认流：技能外化知识，不外化权限。
- **/skill 命令**（main.py 注册 + core/commands.py 参数补全器）：无参列表、
  `/skill <名>` 查看正文（含 token 数）、`/skill reload` 热重载；补全列出技能名。
- **单例语义**：skills.get_loader 按配置签名（enabled/全局目录/项目目录/预算）复用
  或重建，配置变更即时生效；/skill reload 仅重扫目录不改配置。
- **测试**：develop/test_skills.py 17 项全绿（扫描/项目级覆盖/坏技能跳过/清单预算
  降级/懒加载缓存/热重载/禁用短路/工具正文与配套资源/未知名带可用清单/补充注入与
  禁用不注入）；真实链路冒烟：工具表 18 项含 load_skill 且 schema 带公共 description
  参数、policy manual 判定放行、/skill 四分支输出正确、build_messages 头部
  [记忆补充]→[可用技能] 顺序正确。

## 2026-09-29 — 树节点 Enter 切换定案：已展开再按即收回，toggle 后光标钉回原节点

- **问题定性**：树上下文裸 Enter 在 keymap 固定契约中映射为 `Action.EXPAND`
  （core/keymap.py "树上裸 Enter = 展开/折叠"），而 ui 侧 `_tree_expand`
  是**单向展开**——节点已展开时再按 Enter 条件不成立、动作落空，无法收回。
- **双向切换**（core/ui.py `_tree_expand`）：树焦点下无条件 toggle_fold，
  裸 Enter/l/→ 语义统一为「展开↔收起」；`h`/`←`/空格原本即 toggle，现在
  全部树切换键对称。输入焦点下 →/← 仍为光标移动——顺带修复原实现的隐患：
  首分支不查焦点，输入框按 → 若树光标恰停在收起节点上会误切树节点而不动
  光标（折叠改动令 user 节点默认收起后此误触面大增）。
- **光标钉回**（`toggle_fold`）：帮助/计划/步骤等子节点型节点展开会改变
  flat 布局、后续索引整体漂移，「再按一次」会作用到子节点上。toggle 后
  重新展平并按节点 id 把 tree_cursor 钉回被切换的节点（消息类节点子树恒
  walk、布局不变，钉回为幂等空操作）。
- **测试**：新增 develop/test_tree_toggle.py（13 项：Enter 展开→再按收回
  且光标不动、h/l 对称 toggle、帮助节点展开 flat 变长+光标钉回+再按收回、
  输入焦点 →/← 不误切树）；tree_nav 13 项、tree_fold 23 项、tree_refresh
  11 项、tree_autoscroll 8 项、key_binding 33 项、keymap/mouse_wheel/
  paint_throttle/textbuf/input_layout/session_store/backspace_refresh/
  input_cursor_display 及 e2e_mock_llm 回归全绿。

## 2026-09-29 — P1/P2 工具补齐：read 行号化、glob 文件查找、multi_edit 批量编辑

- **read 增强**（core/tools.py）：输出改为 `行号| 内容` 前缀（与 edit_file 变更
  片段、search 上下文块同一视觉语言），模型可直接拿行号锚定编辑；字节级读取加
  NUL 嗅探，二进制文件直接拒绝而非回乱码；解码探测复用 utf-8→gbk 链路，GBK 文
  件此前 utf-8 replace 出乱码，现正确解码（与 edit_file/search 口径一致）；空
  文件返回"（空文件）"而非误导性的"offset 超出范围：文件共 0 行"。
- **glob 工具新增**：文件名模式查找，`**` 跨目录（零层也可），含 `/` 的 pattern
  对相对路径匹配、否则对文件名；24h 内修改的按新→旧置顶、其余按路径，上限 200
  附截断提示；复用 search 的 `_file_matcher`/`_SKIP_DIRS`（原 `_SEARCH_SKIP_DIRS`
  改名共用），非 glob 字符经 re.escape 字面匹配，`a[1].txt` 这类特殊文件名可直
  接命中。
- **multi_edit 工具新增**：单文件多项替换，按数组顺序逐项校验并应用（后项可引
  用前项产物），任一项失败即中止且不写盘（原子），错误标识项序号并附 0 匹配近
  似定位/多匹配行号；全部通过后一次性写回，回显原文件→最终内容的变更片段；逐
  项 replace_all；行尾/编码保留与 edit_file 共用新抽出的
  `_load_editable`/`_save_editable`。
- **配套**：policy.SAFE_TOOLS 加入 glob（只读放行；multi_edit 有写副作用，与
  edit_file 同走确认）；system_prompt 工具清单补 multi_edit/glob 两条指引。
- **测试**：develop/test_p1p2_tools.py 36 项全绿（read 行号/分页/二进制/GBK/空
  文件/越界，glob 基本与跨目录/近期优先/特殊字符/截断/skip 目录，multi_edit 成
  功计数/原子性/顺序依赖/逐项 replace_all/GBK/边界，策略集成）；回归
  test_edit_search 43 项全绿；真实文件锚定闭环冒烟（read 定位→edit→search 验
  证）通过。

## 2026-09-29 — 用词调整：简明记录标签「已剥离」→「已省略」（用户要求）

- 影响模型可见记录与 TUI 显示：memory.finalize_turn 的简明记录前缀改为
  `[已省略·工具名] description call_id=… 完整输出: 路径`；system_prompt.md
  「已省略的调用记录」规则同步改写；develop/考核文档.md、realtest 脚本与三个
  测试脚本的断言字符串同步。机制内部命名（finalize_turn/_strip_tool_message/
  stripped 标志/日志「剥离工具记录 N 条」）不变；白名单预算外置的
  「[白名单外置·…]」标签语义不同，保留原样。历史会话里已落盘的旧
  「[已剥离·…]」记录不迁移（历史事实，模型两种格式均可理解）。
- 验证：context_mgmt / integration / session_store / e2e 四套件全绿；
  全仓 grep 无「已剥离」残留（CHANGELOG/记忆中的历史记录除外）。

## 2026-09-29 — edit_file 反馈层重构 + search 内容搜索工具落地

- **edit_file 重写**（core/tools.py）：此前按 `read_text/write_text` 整体读写，
  LF 文件编辑一次即被 universal newlines 静默改写成 CRLF，非 UTF-8 文件直接抛
  UnicodeDecodeError，失败只回"未找到待替换内容"无定位信息，成功只回"编辑成功"
  无法自校验。现改为字节级读写：解码探测 utf-8→gbk→有损兜底（有损即拒绝编辑防
  损坏），行尾归一匹配后按原文件风格恢复（字节写回不走平台翻译）。
- **失败行号级反馈**：0 匹配时对 old_str 较长行做 difflib 相似度扫描，报近似
  位置行号（L1: 内容），免去整文件重读；多匹配未开 replace_all 时列出每处匹配
  行号，模型扩展上下文即可自行锚定消歧。
- **成功回显变更片段**：定位新旧内容首个差异区间，前后各扩 3 行、超 30 行截断，
  格式"变更片段（第 x-y 行 / 共 N 行）"+ 行号内容；GBK 等非 utf-8 编码保留时
  附注记。
- **search 工具新增**（core/tools.py）：正则内容搜索，按文件分组返回相对路径 +
  行号 + 行文本，参数 pattern/path/glob/context(0-5 上下文块)/max_matches(默认
  50)/case_sensitive。rg 快路径（--json 流式解析，达上限即杀进程；rust 正则不
  兼容退出码 2 → 落兜底）+ 纯 Python 兜底（os.walk + 逐行扫描，支持 GBK，NUL
  嗅探跳二进制，单文件 8MB 上限），两者共用渲染器：context=0 逐行 L行号: 内容，
  context>0 合并相邻匹配为 `--- 路径 Lx-y ---` 上下文块（> 标匹配行），与 read
  的 offset/limit 分页对齐形成"search 定位→read 精读→edit 修改"闭环。跳
  .git/node_modules/__pycache__ 等目录；rg 需 `!**/dir/**` 形式负 glob（绝对
  路径搜索根下 glob 对完整路径匹配，锚根的 `!dir/**` 不生效）。
- **配套接线**：policy.SAFE_TOOLS 加入 search（manual/auto 均免确认）；config
  的 context.large_tools 加入 search（行内上限走 32k 档，超限外置落盘+指针复用
  既有管线）；system_prompt.md"使用你的工具"一节残留的 gemini-cli 工具名
  （read_file/edit/write_file/grep_search/run_shell_command）统一改为实际注册名
  （read/edit_file/write/search/execute_command），glob 条目改为 list_directory。
- **测试**：develop/test_edit_search.py 43 项全绿（真实 rg 14.1.1 双路径等价、
  LF/CRLF/GBK 保留、非文本拒绝、0 匹配近似定位、多匹配行号、片段回显、上下文
  块、glob 过滤、skip 目录、二进制/GBK、截断、无效正则、单文件、注册与策略集
  成）；另对本仓库真实冒烟 SAFE_TOOLS/pointer_line 搜索与 demo 编辑通过。

## 2026-09-29 — 会话树光标导航修复：节点号与行号两套索引经 _node_spans 统一换算

- **问题定性**：树焦点下 ↑↓ 高亮乱跳、滚轮/翻页后光标错位到任意节点。
  根因是索引空间混用——`scroll` 按**渲染行**计数，`tree_cursor` 按**扁平
  节点**计数，而 `_on_up/_on_down/_set_scroll/_clamp_tree_scroll/
  _toggle_focus` 一直互相拿节点号当行号做算术。早期每节点恰渲染 1 行时
  两者等价，消息正文 inline 多行渲染（本次折叠改动后节点行高差异更大）
  后等价关系彻底失效。用户视频取证确认：滚轮大幅滚动为正常操作，
  异常仅在光标导航。
- **换算表**（core/ui.py `_tree_rows`）：渲染时记录 `_node_spans[节点号] =
  (起始行, 行数)`，与行缓存同 key 同生命周期（命中复用、空树重置），
  作为节点空间↔行空间的唯一映射。
- **换算方法**：新增 `_ensure_cursor_visible`（光标节点标题行不可见时最小
  滚动：高于视口的节点顶对齐、否则贴底露出标题；不强求整节点入窗，避免
  与滚轮/翻页互抢滚动位置）与 `_node_at_row`（行→节点，越界夹端点）。
- **导航修正**：`_on_up/_on_down` 只按节点移动光标（保留长按加速步长），
  scroll 可见性统一交给帧循环 `_clamp_tree_scroll`（树焦点下走
  `_ensure_cursor_visible`）；`_set_scroll`（滚轮/翻页/拖拽共用）滚动后把
  光标夹回可见节点区间；`_toggle_focus` 进树光标落尾节点、scroll 交给
  clamp，去掉 `len(flat)-4` 的节点号当行号写法。
- **测试**：新增 develop/test_tree_nav.py（13 项：spans 等长/连续、高亮
  落在光标节点标题行、↑↓ 逐节点移动标题行始终可见、到顶归零/到底贴底、
  滚轮后光标同步进可见节点区间、Tab 进树落尾节点可见）；key_binding 33
  项、tree_autoscroll 8 项、tree_refresh 11 项、tree_fold 23 项、
  mouse_wheel/paint_throttle/keymap/textbuf/input_layout/session_store/
  backspace_refresh/input_cursor_display 及 e2e_mock_llm 回归全绿。

## 2026-09-29 — 会话树长内容折叠落地：消息类节点收起 3 行 + 溢出指示，assistant/plan 全文

- **问题定性**：长消息折叠自消息正文 inline 化重构起即未生效——`_fold`/
  `_COLLAPSE_THRESHOLD`/`max_display_lines` 三个孤儿从未被任何版本接线
  （旧快照 BAWCode_test 中 tool/user/system 的一行摘要+detail 展开范式在
  inline 化时被整体丢弃，折叠能力随之丢失，仅流式思考尾 3 行窗口幸存）。
- **折叠渲染**（core/ui.py `_tree_rows`）：新增 `_TREE_INLINE_CAP = 3` 与
  `_TREE_FOLD_KINDS = {user, tool, system, system_prompt}`（tool 含工具调用
  轮）。可折叠节点收起时正文最多显示 3 个视觉行，超出追加 dim 色
  「… (+N 行)」指示行；marker 仅在正文超限时显示 ▸/▾，短消息保持 ·。
  折行先全量计算再切片，与既有逐帧渲染成本持平；行缓存 key 已含 expanded
  集合，toggle 后正确失效。
- **折叠与子树解耦**（`_flatten_tree`）：折叠类消息节点（user 等回合根）
  收起的只是正文溢出行，其子节点（本回合 Agent 回复/工具记录/流式直播）
  始终 walk 展示——否则 user 默认收起会把整轮对话藏掉。
- **树构建**（`_build_tree`）：user 节点 default_expanded 改 False（短消息
  收起渲染与全文相同，无感知）；help/命令反馈改为 ≤3 行默认展开、更长默认
  收起且子行去掉 [:10] 截断（修复 /model 等短回显被藏）；plan 子行去掉
  [:12] 截断（计划全文不限行数）。assistant 与 stream_content 全文渲染、
  无折叠指示；stream_thinking 尾 3 行窗口原样保留。
- **死代码清理**：删除从未调用的 `_fold()`、`_COLLAPSE_THRESHOLD`、
  `max_display_lines`。
- **测试**：新增 develop/test_tree_fold.py（23 项：长用户消息/工具输出收起
  3 行+指示行+toggle 展开、回合子树不随折叠隐藏、assistant 30 行与 plan
  20 行全文、流式思考尾窗、help 短可见长收起）；回归 test_tree_refresh 11
  项、test_tree_autoscroll 8 项、paint_throttle/mouse_wheel/key_binding/
  keymap/textbuf/session_store/backspace_refresh/e2e_mock_llm 全绿；
  test_input_cursor_display 修复陈旧 buffer 赋值（适配 TextBuffer API）。

## 2026-09-29 — 压缩摘要结构化：qwen 式 state_snapshot 提示词 + 程序解析校验

- **提示词**（core/prompts/compress.md 重写）：参考 qwen-code 0.24.1
  `getCompressionPrompt()`（packages/core/src/core/prompts.ts）的两段式结构——
  先 `<analysis>` 草稿块（按时间线梳理请求/决策/细节/错误/用户反馈，生效前被
  程序剥离），再严格输出 `<state_snapshot>` XML（9 节：primary_request_and_
  intent / key_technical_concepts / files_and_code_sections / errors_and_fixes /
  problem_solving / all_user_messages / pending_tasks / current_work /
  next_step，中文注释说明各节要求）。保留管线既有设定：`^{summary_token_target}^`
  占位符（程序按旧段 5% 钳制 [150,800] 计算）、"摘要 + 最近几轮原文"框架、
  用户原话最高优先级逐条保留。
- **解析器**（core/memory.py 新增模块级 `parse_state_snapshot()`）：剥除首尾
  代码围栏与 `<analysis>` 草稿块 → 提取 `<state_snapshot>` → 按 9 节逐一
  抓取 → 至少 3 节非空才有效（防模型原样回显模板注释的空壳结构）。
- **管线接入**（memory.compress）：钩子纯文本契约不变（能解析则结构化、不能
  则按原样接受）；LLM 路径强制结构化——解析失败自动重试一次（重试提示词明确
  指出缺失结构），仍失败走 `_compress_fail` 熔断、历史保持不变，不可解析的
  摘要绝不入库。摘要 token 上限改按剥除草稿后的 XML 计；摘要消息新增
  `sections` 字典（程序可直接读取各节）与 `metadata.structured` 标记。
- **测试**（test_context_mgmt.py 断言 12 组新增）：解析容错（无结构/全空壳/
  围栏与草稿剥除）、结构化成功（提示词含模板+历史、产出剥除草稿、摘要+尾段
  替换、sections 可读、旧段归档）、失败重试一次后成功、两次不可解析则失败
  不动历史且熔断计数。连同既有断言共 51 项全绿；其余 4 套件回归无恙。

## 2026-09-29 — 流式输出适配：SSE 双路径聚合 + 树尾直播 + 阶段状态行

- **llm 层**（core/llm.py）：`chat()` 新增 `on_delta(kind, piece)` 回调（kind ∈
  reasoning/content）；config `llm.stream`（默认 true）开启时走 SSE 流式——
  SDK 路径 `_native_chat_stream`（stream=True + stream_options include_usage，
  逐 chunk 聚合回调）与 urllib 降级路径 `_http_chat_stream`（SSE 行迭代解析，
  请求体补 stream 字段；Content-Type 非 event-stream 时同一聚合器优雅降级）。
  聚合器 `_StreamAggregate` 产出伪 OpenAI 响应复用现有 `_normalize`（arguments
  解析/id 补齐/reasoning 分流零改动），usage 从终块进现有计量。取消：每 chunk
  检查 _cancel，cancel() 增加关闭 urllib 在途句柄 `_inflight_resp`（补上原盲区）。
  失败语义：首个 chunk 前失败自动回落非流式（本次尝试内）；已产出后中断 midway
  定格不重试（partial 已上屏），错误随结果返回。`_build_client` 显式
  max_retries=0（消除 SDK 默认 2 次 × 外层 3 次的双层重试叠加）。
- **树尾直播**（main.py + core/ui.py，按用户图示规格）：流式对象为纯 UI 侧属性
  `app.streaming_msg`（agent 线程单写者、不进 session.messages——半成品不落盘/
  不进 API，中断丢弃语义与在途回合一致）；`_build_tree` 生成两个直播节点——
  思考过程（kind=stream_thinking，**过长折叠为尾部 3 行动态窗口**，随流式滚动）
  与正文（stream_content，全文折行）；贴底跟随沿用 follow_tail。签名加流式
  长度使每帧失效重画。消息配色为**用户指定色值**：用户消息 3ca2a2、Agent 消息
  （含流式正文与完成态）80944e、思考过程 045f62（新增主题键 user_msg/agent/
  thinking）；工具调用节点双色渲染——标题 14babc + 内容纯白（同一物理行双色，
  tool_call 轮节点 kind 改 tool 与工具结果一致；新增主题键 tool_title/
  tool_content），DEFAULT_COLORS 为默认值、主题文件可覆盖。
- **阶段状态行**（用户图示规格）：树底常驻 1 行（tree_h 预算 -1）——产出阶段
  "思考中"（红/err 色）、工具执行"工具调用中"、完成空行占位（布局稳定）；
  main 在轮循环 chat 前/工具执行前/完成分支设置，wrapper finally 兜底清空。
- **测试**（develop/mock_llm_server.py + test_e2e_mock_llm.py 扩展）：mock 支持
  SSE（reasoning 2 片/content 3 片/arguments 3 片/usage 终块/[DONE]，逐帧
  flush + 可配延迟）、reject_stream 强制回落。场景二 13 项断言：同脚本流式/
  非流式会话逐字段等价（对拍不变式）、on_delta 逐片累积快照、SSE 多片切分、
  阶段行变迁、出站 payload 无 streaming 半成品、usage 计量、urllib SSE 路径
  等价、回落、中途取消（Timer 注入 → 注记中断/partial 丢弃/树尾清空/日志取证）。
  修复：urllib 流式请求体缺 stream 字段导致静默空响应（SSE 解析器静默跳过
  整段 JSON——现加 Content-Type 守卫 + 请求体补字段）。无头渲染冒烟：思考
  尾窗折叠/正文节点/阶段行/收口无残留。全量回归 5 套件全绿。

## 2026-09-29 — 端到端测试基建：本地 OpenAI 协议 mock LLM 服务器 + 日志分析断言

- `develop/mock_llm_server.py`（新增，可独立复用）：stdlib 实现的 OpenAI 兼容
  `/v1/chat/completions` 服务器——按脚本 JSON 顺序返回预定内容（支持
  reasoning_content / tool_calls），带 tools 的 agent 请求与无 tools 的内部调用
  分流路由，脚本耗尽返回保底响应（靠请求数断言发现超发）；每个请求的完整
  payload 记录到 JSONL。独立运行：`python develop/mock_llm_server.py --port
  8123 --script s.json --log req.jsonl`，也可被真实 TUI 配置指向做真机测试。
- `develop/test_e2e_mock_llm.py`（新增）：临时目录隔离环境（配置 base_url 指向
  mock、log.level=debug、full 权限模式），驱动两个真实回合（list_directory →
  read 4.6k-token 大文件触发超限外置 → 收尾；第二回合直接收尾），LLM 链路全程
  真实 HTTP（openai SDK/urllib → localhost）。断言全部来自"分析日志结果"，
  三类证据 30 项：A 出站 payload（请求 JSONL，A1-A12：请求数/工具定义注入
  description/思维链逐轮并回/description 剥除/超限指针与行内截断/跨回合思维链
  零残留/简明记录格式/<200 字守卫）；B 应用日志段（按字节偏移截取本次运行，
  B1-B8：复杂度启发式/回合完成/真实调用计数/工具执行/外置落盘/回合末维护计数）；
  C 磁盘产物（C1-C6：外置记录元数据+完整输出、会话存档 thinking 保留与剥离标）。
- 教训：日志文件含中文（UTF-8 多字节），`st_size` 是字节而 `read_text()` 切片
  是字符——按偏移截取日志段必须两侧同单位，否则中文文件切片恒为空。
- 顺带验证了既有行为在真实协议下的表现：<200 字工具输出守卫（ls 结果原样
  保留）、reasoning_content 经 openai SDK `model_dump()` 保留、日志段单位。
- 验证：e2e 30 项全绿（真实 HTTP 往返 4 次 <1.5s）；context_mgmt /
  context_turn_integration / session_store 套件回归无恙。

## 2026-09-29 — thinking 生命周期定稿：回合内拼接思维链，跨回合剥离（对齐 DeepSeek 多轮工具调用规范）

- 目标形态（用户给定图）：单回合内每次请求携带之前各轮思维链（请求 1.2 输入含
  思维链 1.1，请求 1.3 含 1.1+1.2）；发起第二轮对话起，上一回合思维链不再进上下文。
- 相对上一条目的修正（`core/llm.py`）："仅思考无正文顶替 content" 改为按
  **是否携带 tools 分流**——agent 工具轮（`keep_reasoning=True`）不顶替，思考一律进
  thinking 字段（否则仅思考轮的思维链以 content 身份入库，回合末无法识别剥离）；
  无工具的内部文本调用（计划/步骤/复杂度判定/压缩/refine）保持顶替兜底，下游取文本
  零回归。
- 剥离实现（`core/memory.py`）：`finalize_turn` 回合末给所有 assistant 消息的
  thinking 字段打 `thinking_stripped` 标——**会话记录保留，仅停止出站并回**；
  `_api_session_item` 遇标跳过合并。幂等；正常结束/出错/Ctrl+C 全路径生效。
- 收尾轮守卫放宽（`main.py`）：`content or reasoning` 非空即入库——仅思考收尾
  也有记录（content 为空 + thinking 字段）。
- thinking 仍无行内限额/保护条数（与工具记录不同），回合内多轮长思考会累积，
  80% 压缩闸兜底；回合内 thinking 随每轮请求全量回传（含计费）。
- 验证：test_context_mgmt.py 增 3 项（工具轮不顶替/剥离标后不并回/回合末打标），
  test_context_turn_integration.py 增 3 项改 1 项（回合内请求 1.2 已并回思维链、
  回合末双消息打标、第二回合不再并回且 payload 无任何思维链残留）全部通过；
  session_store/keyinput 回归无恙。

## 2026-09-29 — thinking 留档：思考与正文并存时不再丢弃，出站并回 content

- 背景：此前 `_normalize` 对 reasoning_content 是"二选一"——正文非空时思考直接
  丢弃，仅思考时顶替 content。逐轮对话中"思考+正文"并存轮的思考就丢了。
- 改动（`core/llm.py` / `main.py` / `core/memory.py`）：
  - `_normalize` 拆分返回 `content` + `reasoning`；**仅思考无正文仍顶替
    content**（保住计划/压缩/复杂度判定等下游取文本的既有路径不回归）；
  - assistant 入库点（工具轮消息 + 收尾消息）并存时以 `thinking` 字段随消息
    留档——会话记录/UI 载荷保持 content 干净，思考单独成字段；
  - `_api_session_item` 出站时把 thinking 并回该条 assistant 消息的 content
    （`思考\n\n正文`），跨轮/跨回合模型都能看到自己之前的思考；
    thinking 仅存记录不出站的裁剪开关留给后续按需加。
- 回合结束判定与工具结果回填机制不变：无 tool_calls 即回合结束；有
  tool_calls 则结果照旧回填上下文。
- 验证：test_context_mgmt.py 增 5 项（normalize 拆分/顶替、出站并回/仅思考/
  无字段不受影响）+ test_context_turn_integration.py 增 3 项（工具轮与收尾
  消息 thinking 留档、第二回合 payload 中已并回）全部通过；
  session_store/keyinput 回归无恙。

## 2026-09-28 — 上下文管理系统：工具白名单 + 回合末剥离 + 结果外置磁盘

- 核心链路（core/toolstore.py 新增 + memory.py 重构）：每次工具调用生成专有
  call_id；结果行内限额（普通工具 4096 tokens / read·write·edit_file 32768
  tokens，tiktoken 按模型 encoding 计，缺失退化 chars//2），超限部分立即以
  call_id 为文件名原子写入 `data/toolcalls/{project_id}/{session_id}/`，
  行内保留头部切片 + 落盘指针（模型可用 read 工具 offset/limit 分批回读）。
- 白名单机制（config `context.tool_whitelist`，默认 7 个状态/记忆类工具：
  write_plan/update_plan/generate_steps/update_step_status/memory_add_fact/
  memory_add_project_note/rag_add）：完整调用历史跨回合保留，会话累计超过
  `whitelist_budget_tokens`（默认 32768）时最老的先外置。
- 白名单外工具回合末剥离（`Memory.finalize_turn`，_agent_turn 的 try/finally
  保证正常结束/出错/Ctrl+C 全路径执行）：content 改写为简明记录
  `[已剥离·工具名] description call_id=… 完整输出: 路径`，消息本体保留
  （tool_call/tool_result 配对安全）；结果未落盘的先落盘；<200 字的短内容
  （拒绝原因/短错误）不改写；最近 `strip_keep_recent`（默认 6）条保护不剥离。
- description 参数（评审 P0 闭环）：get_tool_defs() 为全部工具统一注入公共
  `description` 参数（required）——模型传"本次调用意图"一句话；main 提取后从
  arguments 剥除（策略指纹/确认面板/工具执行不可见，execute_approved_tool 双
  保险），随 tool 消息留档；缺失时兜底 `工具名(参数摘要≤80字)`。
- 系统提示词新增三条规则（description 必填 / 落盘指针与 read 分页回读 /
  已剥离记录语义）；修复原文残留的 `<persisted-output>` 幽灵描述（此前提示词
  有此机制描述但代码从未实现，本次按实际实现改写）。
- 退役：`Memory.strip_old_tool_messages`（轮中就地保留最近 6 条）删除——与新
  规格"回合内完整可见、回合末才剥离"冲突；`memory.strip_tool_history/
  strip_tool_keep` 配置废弃不再读取；maybe_compress（窗口 80%）保留兜底，
  会话级压缩不在本次范围。/clear 顺带清理外置记录目录（含空项目目录）。
- read 工具增强：可选 offset（起始行）/limit（行数）行分页，向后兼容；
  分页输出首行标注（第 a-b 行 / 共 N 行）。
- 验证：develop/test_context_mgmt.py（31 项：schema 注入/剥除/溢出外置+指针/
  剥离保护/白名单预算最老先外置/幂等//resume 往返/read 分页/clear 清理）与
  develop/test_context_turn_integration.py（monkeypatch LLM 全链路：两轮脚本
  响应驱动 _agent_turn，验证提取→执行→落库→回合末自动剥离→第二回合 payload
  已是简明记录）全部通过；test_session_store.py 等既有套件回归无回归。
  已知存量问题（与本改动无关）：test_context_injection.py「系统提示词含
  harness/工作区」断言失败——用户在途的系统提示词重写已移除全部 ^{占位符}^
  （渲染不再含 "BAWCode" 字样），该断言在本次改动前即失败。

## 2026-09-28 — LLM 对话异步化：等待响应不再阻塞 TUI

- 架构：`_agent_turn` 移入后台线程（`main._AgentRunner`），主循环始终停留在
  `read_line` 帧循环——等待 LLM/工具执行期间可自由滚动、使用指令、输入消息。
  agent 线程只改状态不渲染（`_sync(render=False)`），画面统一由主循环帧渲染。
- 交互弹窗经 UI 请求桥移交主线程：`app.request_ui(kind, payload)` +
  `wait_ui(req, cancelled)`，`read_line` 帧循环内 `_serve_ui_request()` 执行
  （confirm 面板/choose 计划确认/计划编辑行输入），单读者保证不与主输入竞争。
- 两种发送模式（设置页「系统 · 等待时新消息」，`ui.busy_send_mode`，默认 queue）：
  - `queue`——消息入队（树中显示 `[已排队 N 条]` 提示，type=help 不进 API），
    本轮结束后由 runner 接力依次执行；
  - `interrupt`——`llm.cancel()`（置标记 + 关闭 openai/httpx 在途连接，立即生效）
    丢弃当前回合，强行以新消息开启新一轮；`chat()` 在入口/重试/异常检查点返回
    `__CANCELLED__`，`_agent_turn` 各检查点清理并注记 `[本轮已被新消息中断]`。
- 会话级命令（/clear /new /resume）在回合进行中拦截并提示；/exit 走
  `cancel_and_join()` 收尾。计划流程的 choose/read_line 全部桥接化。
- 已知边界：urllib 降级路径（无 SDK/key）的中断在当前 HTTP 返回后才生效；
  回合进行中 save_session 只在回合结束/收尾时执行。
- 验证：py_compile + `import main`；7 套件回归全绿；无头状态机冒烟
  （队列语义/中断语义/chat 取消哨兵/UI 桥回路/桥取消兜底）全部通过。

## 2026-09-28 — 鼠标修复二段：翻译层大小写匹配（取证定位）

- 取证（develop/pt_input.log，子代理分析）：`started reader=BawWin32Input` 证明
  事件路径已生效，终端投递正常（96 条鼠标事件，滚轮/拖拽轨迹完整、坐标合理），
  但 100% 被 drop——pt `_handle_mouse` 的 data 为**大写**枚举值
  （`'NONE;SCROLL_UP;76;19'`），翻译层按小写匹配全部落空。
- 修复：`_mouse_event` 统一 `strip().lower()` 后匹配；`MOUSE_UP` 的 button 为
  NONE 不参与按下态判定。单测同步改为真实大写格式为主（此前用自编小写样例
  自证了错误假设）+ 小写兼容用例。
- 教训：合成测试用例的数据必须取自真实源码/取证，不能凭假设编造；
  `pt 枚举 .value` 大小写要用运行时取证确认。
- 取证日志（BAW_LOG_KEYINPUT）暂保持默认开启，真机复验通过后改回 opt-in。

## 2026-09-28 — 确认框重设计：面板取代输入框 + 修复"无法输入"

- 根因（无法输入）：`_confirm_loop` 用内建 `input("请选择> ")` 读选择——新输入栈
  raw_mode 关闭了 ENABLE_LINE_INPUT/ECHO（input() 无行缓冲/回显），且后台泵线程
  持续 ReadConsoleInputW 抽干控制台输入（字符被偷），双重失效。设置页 3 处
  `input()`（文本项/新增 provider/model）同理是地雷。
- 重设计（用户需求）：废弃 `_confirm_loop`，新增 `TuiApp.show_confirm_form`——
  确认面板**取代输入框位置**渲染（行数多时向上拓展压缩会话区），不再是屏幕最底部；
  **↑↓ 选择**选项（滚轮也可），Enter 确认；选中「拒绝并输入原因」进入行内输入态
  （块光标跟随，Enter 提交=空则默认理由，Esc 返回选项）；保留 1/2/3 快捷键；
  Esc/Ctrl+C = 默认拒绝。main.py `_handle_tool_confirm` 改接面板
  （返回 {"action","reason"}，不再用 `__ALLOW_*` 哨兵）。
- 内建 input() 救济：`keyinput.paused()` 上下文管理器（停泵线程 + 退出 raw 恢复
  行缓冲，退出时清残留输入），ui.py 新增 `_paused_input` 并替换设置页 3 处
  `input()` 调用点。
- 面板渲染：`_compose_plain` 布局注入（`confirm_panel` 取代输入区行位），
  `TextBuffer.cursor` 为 property（非方法）。
- 验证：面板选项/原因模式渲染冒烟通过；7 套件回归全绿；`import main` 正常；
  真机复验点：确认框出现时直接打字选择、原因输入、Esc 行为。

## 2026-09-28 — 修复鼠标失效：强制事件记录路径 + pump 防空转

- 根因：pt 的 `_is_win_vt100_input_enabled()` 在 Win10+ 终端试探成功后切换到
  Vt100ConsoleInputReader（VT 字节流路径），其 `_get_keys` 只解码 KEY_EVENT，
  MOUSE_EVENT 记录被整类丢弃 → 滚轮/滑块拖拽全断；VT 路径的鼠标需应用主动
  发 `\x1b[?1000h` 上报序列（原型因 `PromptSession(mouse_support=True)` 曾
  可用，集成后无 pt Application 故失效）。
- 修复（方案一）：`core/keyinput.py` 新增 `BawWin32Input` 子类，强制
  `_use_virtual_terminal_input=False` 并使用 ConsoleInputReader——回到旧栈
  同源的事件记录路径：MOUSE_EVENT → WindowsMouseEvent → 现有翻译层；
  raw_mode 不再设置输入 VT，与 `_enable_windows_ansi` 只开输出 VT 的设计一致。
- 顺带修复 `_pump` 热循环：pt 的 `read()` 非阻塞（wait_for_handles timeout=0），
  空转时 `sleep(0.01)` 防占满 CPU 核。
- 测试：`develop/test_keyinput_pt.py` 增 3 项（工厂/关闭 VT/reader 类型），
  58 项全绿；既有套件回归通过。
- 真机复验点：滚轮滚动、滚动条拖拽、中文 IME（解码路径切换后按惯例复验）。

## 2026-09-28 — 会话指令重定义：/resume /new /clear /rename

- `/sessions` 更名 `/resume`（↑↓ 选择 · Enter 加载 · d 删除 · Esc 取消）；
  面板移除「＋新建会话」行，新建改由独立命令承担。
- 新增 `/new`：保存当前会话并开启新会话（承接原面板 new 动作/旧 /clear 语义）。
- `/clear` 语义重定义：清空当前会话内容，原地恢复为空对话（保留会话 id），
  并删除已落盘文件防止 /resume 复活旧内容；不再先存档再换新。
- 新增 `/rename <标题>`：手动命名当前会话，取代自动截取首条用户输入命名——
  save_session 不再派生标题，未命名会话在 /resume 列表显示「（无标题会话）」。
- `develop/test_session_store.py` 断言同步（手动命名/清空用例，25 项通过）。

## 2026-09-28 — 键位定案：Enter 直接发送，Ctrl+Enter 换行

- 用户决策：reader 子类恢复 Shift+Enter/Ctrl+Enter 修饰位的方案实现较复杂、
  可能引入新问题，不采用（相关草稿已撤销）。
- `core/keyinput.py`：Escape+ControlM 折叠由 submit_ctrl 改为 **newline**——
  Ctrl+Enter 物理键 → 换行；Ctrl+J 保持换行（同形 ControlJ，VT 路径下
  Ctrl+Enter 落为 \n 亦兼容）；**Enter 直接发送**；Shift+Enter 与 Enter
  同形 → 提交（事件流固有限制）；Alt+Enter 同形 → 换行。
- submit_ctrl 事件不再产生：keymap 的 `send=ctrl+enter` 绑定随之闲置
  （如需独立发送键可配置 `send=f5` 等，Enter 始终发送）；
  `ui.newline` 配置值现为标签，物理键固定。
- `develop/test_keyinput_pt.py` 断言同步（Ctrl+Enter/树上/回退用例改指 NEWLINE）。

## 2026-09-28 — PT 输入栈转正：input_pt.py 更名 core/keyinput.py，旧实现入回收站

- 真机验证通过后正式切换：`core/input_pt.py` → `core/keyinput.py`（自包含：
  本地定义 KeyEvent/TICK，去除对旧模块的依赖）；旧实现（msvcrt + 自研
  解码/FSM/滴灌）迁入 `_recycle/keyinput.py`，其内部单测随之移入
  `_recycle/develop/`（test_keyinput_unit / test_keyinput_decoder /
  test_ime_bytes / minimal_repro）。
- ui.py 摘除双后端开关（`_input_mod`/`_resolve_input_backend`），读取入口
  直接走 `core.keyinput`；config 默认值还原；prompt_toolkit 转正为必需依赖
  （requirements `>=3.0.53`）。
- 快捷键兼容性（翻译层 → keymap 解析集成验证，55 项无头测试）：
  Enter→SUBMIT、Ctrl+Enter→SEND、Ctrl+J→NEWLINE、Shift+Tab→MODE_CYCLE、
  Tab→COMPLETE/FOCUS_NEXT、PageUp/滚轮→SCROLL、编辑键→CARET_*/BACKSPACE/
  DELETE/ESCAPE、字符→INSERT、可配置 send=f5 命中 SEND、Ctrl+Up→SCROLL_UP
  全部与旧栈一致。interrupt 由读键循环消费（ui.py 1367）、clear 的 ctrl+u
  token 在编译表无落点均为旧栈既有行为，事件级两栈一致。
- 测试改造：test_input_pt_adapter → `develop/test_keyinput_pt.py`（追加
  keymap 集成段）；test_key_binding 摘除旧栈 Decoder/KeyReader 段；
  test_mouse_wheel 的 SGR 字节段替换为 Win32 鼠标 data 形态断言；
  test_context_injection 源码断言更新。
- 已知语义差异（不变）：Shift+Enter 物理键在事件流下不可区分，Ctrl+J 为
  换行候选（旧栈该键被丢弃）；Ctrl+Enter 折叠回 submit_ctrl 语义不变。

## 2026-09-28 — PT 输入后端（Phase 1）：keyinput 可切换替代实现

- 新增 `core/input_pt.py`：prompt_toolkit（3.0.53）输入后端，公开契约与
  keyinput 完全一致（read_key_event/read_events/read_key/flush_input/
  direction_of/supported_kinds + 同一 KeyEvent 类型）；数据源同为 Win32
  INPUT_RECORD 事件流，差异只在翻译层。
- 翻译层：KeyPress → KeyEvent 纯函数（`translate_key_presses`），枚举键
  先于 str 判断（Keys 是 str 混入枚举）；序列折叠 Escape+ControlM→submit_ctrl、
  Escape+可见字符→char（alt 前缀对齐 legacy）；BracketedPaste 自带 data 或
  同批可见字符组装为 paste；WindowsMouseEvent "button;type;X;Y" → mouse_* 事件
  （坐标 0-based 单元格，与 legacy 协议一致）。
- 读取循环：后台线程阻塞读键 + 队列，`read_events(0)` 非阻塞供拍帧泵；
  raw_mode 与 legacy get_reader 同生命周期（启动进入，进程结束释放）。
- 切换开关：`config.json` `system.input_backend = "legacy" | "pt"`
  （默认 legacy，行为零变化）或环境变量 `BAW_INPUT_BACKEND=pt`；
  ui.py 读取入口（_read_key/_read_events/_key_direction/_flush_input）
  统一走后端选择，bind_config 时解析。
- 已知差异（原型真机验证结论）：Shift+Enter 事件流下与 Enter 不可区分，
  Ctrl+J 映射为 newline 作为换行候选（legacy 路径该键被丢弃）。
- 测试：`develop/test_input_pt_adapter.py` 翻译层 30 项无头单测全绿；
  既有 6 套件（layout/keymap/textbuf/session_store/mouse_wheel/paint_throttle）
  回归全绿；prompt_toolkit 为可选依赖（requirements 注释行）。

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

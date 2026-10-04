"""hello-plugin —— BAWCode 外部插件官方示例。

演示五类扩展点的接入方式（详见 docs/plugin-development.md）：
  1. 斜杠命令     /hello <名字>
  2. 模型可见工具 plugin_hello（走统一权限/确认/上下文限额管线）
  3. hook 事件    after_tool / before_turn 观察，context_supplement 上下文补充
  4. 技能         skills/hello-usage/SKILL.md 自动并入技能清单
  5. 配置声明     plugin.json config 数组 → 设置面板"插件"标签页可视化编辑，
                  ctx.settings 实时读取（改动即时生效，无需重载）

插件私有配置持久化在 config.plugins_config["hello-plugin"]；未配置项回落
清单声明的缺省值。把本目录整个复制改名，即是新插件的起点。
"""


def setup(ctx):
    # ctx.log 已按插件 id 命名空间化（plugins.hello-plugin）
    ctx.log.info("hello-plugin 装载，配置=%s", ctx.settings)

    # ---- 1. 斜杠命令：handler(ctx, args)，ctx 含 llm/session/config/app ----
    def _hello(cctx, args):
        name = args or ctx.settings.get("greet") or "world"
        style = str(ctx.settings.get("style") or "简洁")
        marks = "!" * int(ctx.settings.get("shout") or 1)
        line = f"hello, {name}{marks}"
        if style == "详细":
            line += "（来自 hello-plugin · 详细模式）"
        return line

    ctx.register_command(
        "/hello",
        hint="示例插件：问候",
        usage="/hello <名字>",
        handler=_hello,
    )

    # ---- 2. 模型可见工具：进 register 注册表，走统一权限/确认管线 ----
    @ctx.register_tool(
        name="plugin_hello",
        description="Example tool from the bundled hello-plugin: build a greeting line. Use only when the user explicitly asks to try the plugin demo tool.",
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Who to greet"},
            },
            "required": [],
        },
    )
    def plugin_hello(name: str = "") -> str:
        target = name or str(ctx.settings.get("greet") or "world")
        return f"plugin_hello: hello, {target}!"

    # ---- 3. hook 事件：观察链（返回 None 即不参与改写）----
    def _on_after_tool(payload):
        ctx.log.debug("after_tool: %s (%.3fs)", payload.get("name"), payload.get("elapsed") or 0)

    def _on_before_turn(payload):
        # 变换链：返回 None 表示不改写 user_text
        ctx.log.debug("before_turn: %s", (payload.get("user_text") or "")[:50])

    ctx.register_hook("after_tool", _on_after_tool)
    ctx.register_hook("before_turn", _on_before_turn)

    # ---- 4/5. 上下文补充：实时读 supplement 开关（设置面板/配置改动即时生效）----
    def _supplement(payload):
        if not ctx.settings.get("supplement"):
            return None
        return "hello-plugin 示例补充：此行为插件经 context_supplement 注入。"

    ctx.register_context_supplement(_supplement)

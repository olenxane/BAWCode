from bawcode_sdk import PluginContext


def setup(ctx: PluginContext):
    def _hello(cctx, args):
        return f"{{PLUGIN_ID}}: hello{(' · ' + args) if args else ''}"

    ctx.register_command(
        "/{{PLUGIN_CMD}}",
        hint="{{PLUGIN_DESC}}",
        usage="/{{PLUGIN_CMD}} [参数]",
        handler=_hello,
    )
    ctx.log.info("{{PLUGIN_ID}} 装载完成")

"""示例命令插件：演示 handler 与元数据解耦

将本文件放到 data/commands/ 即可被 commands.load_plugins() 加载。
可定义 setup(commands) 或直接 import 后调用 register。
"""

# 元数据可先注册，便于补全；执行器可稍后由宿主提供
hint = "插件示例：回显参数"


def setup(commands):
    # 仅元数据（无 handler → 补全可见，执行提示 no_handler）
    commands.set_meta(
        "/plugin_ping",
        hint="插件占位：显示 pong",
        usage="/plugin_ping",
        source="plugin-example",
    )
    # 完整注册：handler + 元数据
    commands.register(
        "/plugin_echo",
        hint="插件：回显参数",
        usage="/plugin_echo <text>",
        source="plugin-example",
        handler=lambda ctx, args: f"echo:{args}",
    )

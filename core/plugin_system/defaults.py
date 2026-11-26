"""插件默认启用策略

非必要插件默认禁用：不装载、不检查/安装依赖，也不占用启动时间。
用户通过 /plugin enable <id> 或设置页启用后，依赖才会异步补齐并装载。
"""

DEFAULT_DISABLED_PLUGINS = [
    "music-player",
    "computer-use",
    "qbridge",
    "remote-control",
    "hello-plugin",
    "bottom-banter",
]

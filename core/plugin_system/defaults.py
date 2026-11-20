"""插件默认启用策略

非必要插件默认禁用：不装载、不检查/安装依赖，也不占用启动时间。
用户通过 /plugin enable <id> 或设置页启用后，依赖才会异步补齐并装载。
"""

# 默认禁用清单（config.plugins.disable 缺省时生效）
# - music-player：重依赖（numpy/scipy/sounddevice/ffmpeg），启动耗时明显
# - computer-use：依赖 pyautogui/pygetwindow，无桌面环境时不可用
# - qbridge：需要外部 OneBot 服务
# - remote-control：会监听本机端口，按需启用
# - emotion-detector / hello-plugin / bottom-banter：演示与趣味插件
DEFAULT_DISABLED_PLUGINS = [
    "music-player",
    "computer-use",
    "qbridge",
    "remote-control",
    "emotion-detector",
    "hello-plugin",
    "bottom-banter",
]

# 终端音乐播放器插件

把 terminal-music-player 的本地播放部分移植为主窗口底部插件：底部实时显示
歌曲/进度/歌词/频谱，用 `/music` 指令代替原来的键盘操作。

播放器核心为 `tmplayer/`（从原项目 `mp/` 复制，仅保留本地播放链路：解码、DSP、
歌词、频谱、统计）。在线音乐部分未移植。

## 安装与配置

- 依赖见 `requirements.txt`（一般随主程序环境已具备）。
- 首次装载会把 `config.yaml` 复制到 `{工作区}/.bawcode/plugin-data/music-player/`，
  之后所有配置改动写在该副本，不改动插件目录内的模板。
- 音频目录、歌词目录、主题、均衡器、DSP 等均在 `config.yaml` 中配置；改完执行
  `/music reload` 热重载。

## 指令

| 指令 | 说明 |
|------|------|
| `/music` / `/music status` | 播放状态 |
| `/music play [关键词\|序号]` | 点播；不带参数=继续/恢复当前 |
| `/music pause` / `toggle` / `stop` | 暂停 / 播放暂停切换 / 停止 |
| `/music next` / `prev` | 下一首 / 上一首（含队列与历史） |
| `/music list [关键词]` | 列出曲库（关键词过滤） |
| `/music add <关键词\|序号>` | 加入播放队列 |
| `/music queue` / `clearqueue` | 查看 / 清空队列 |
| `/music vol [0-150]` | 查看 / 设置音量 |
| `/music seek <±秒\|mm:ss>` | 定位 |
| `/music mode [seq\|shuffle\|one\|all]` | 播放模式 |
| `/music spectrum` | 频谱开关 |
| `/music theme [名称]` | 配色主题 |
| `/music lyric` | 当前歌词上下文 |
| `/music dir [list\|add <目录>\|rm <序号>]` | 音乐目录管理 |
| `/music rescan` | 重扫曲库 |
| `/music reload` | 重载 config.yaml |
| `/music close` / `open` | 关闭 / 恢复底部显示 |

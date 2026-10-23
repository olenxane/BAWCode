# qbridge —— QQ 桥接插件

经 OneBot 11 正向 WebSocket 连接 QQ 机器人框架，把终端与 QQ 双向同步：终端上的对话与过程动态镜像到 QQ，QQ 收到的消息（含图片）作为输入注入终端，确认/选择框可映射到 QQ 远程应答。插件入口使用随宿主提供的 `bawcode_sdk`，不直接导入 `core` 实现；SDK v1 清单声明为 `"sdk_version": 1`。通用接口及迁移约定见[插件开发指南](plugin-development.md#15-sdk-v1架构宿主服务与迁移)。

## 快速开始

1. 准备一个 OneBot 11 实现的 QQ 机器人框架（NapCat / LLOneBot / Lagrange.OneBot / go-cqhttp 等），在框架侧**开启正向 WebSocket 服务端**（如 NapCat 的 "WebSocket 服务器"，端口例 3001）。
2. `/settings` 打开设置面板 → "插件" 页 → qbridge，填写：
   - `ws_url`：框架的 WS 地址（默认 `ws://127.0.0.1:3001`）
   - `access_token`：框架设置了访问令牌时填写（可选，留空不带鉴权头）
   - `self_id`：机器人 QQ 号（可选，留空则连接后经 `get_login_info` 自动获取）
   - `target`：同步目标 `private/<你的QQ号>` 或 `group/<群号>`（可选，留空自动绑定首个消息来源）
   - `allow_users`：**控制白名单**（逗号分隔 QQ 号）
3. `/qbridge on` 启动；`/qbridge status` 查看连接状态。

## 同步语义

| 方向 | 内容 | 说明 |
|---|---|---|
| 终端 → QQ | 助手回复 | 回合结束下发，超长按 `max_len` 分段 |
| 终端 → QQ | 终端侧输入的用户消息 | `[终端]` 前缀；粘贴图片随消息链转发 |
| 终端 → QQ | 系统消息（命令回显/插件通知） | `[系统]` 前缀，`sync_system` 可关 |
| 终端 → QQ | 工具执行活动 | 每次一行摘要，`sync_tools` 可关 |
| QQ → 终端 | 白名单用户的文本 | 等价输入框发送：idle 开新回合，busy 排队为当前回合结束后的新回合 |
| QQ → 终端 | 白名单用户的图片 | 下载到 `.bawcode/plugin-data/qbridge/qq-images/`，走宿主多模态管线（非视觉模型时保留路径文本） |
| 双向代答 | 工具确认框 | QQ 回复 `1`=本次允许、`2`=总是允许、`3 拒绝 [原因]` |
| 双向代答 | 选择框 / 行输入 / 询问面板 | QQ 回复编号或文本（与键盘输入同语义） |

## 交互等待与手动接管

QQ 侧应答**不设超时**——终端会一直等待，直到：

- QQ 回复合法应答（无效回复会收到格式提示并继续等待）；
- **终端用户提交了新消息**（手动接管）：等待释放、交互转回终端面板，终端面板自身的取消/超时机制不受影响；已提交的消息按排队语义在本回合结束后续跑；
- `/qbridge off` 或 WS 断开：待应答全部转回终端面板。

## 安全模型

- **严格白名单**：仅 `allow_users` 名单内的 QQ 号可提交消息/应答交互；名单外的消息一律忽略。**名单为空 = 只读镜像**（QQ 收得到推送，但任何消息都不注入终端）。
- 机器人自身消息（self_id，含其他端登录）自动过滤；经 QQ 提交的文本不会回发到 QQ（防回声/自激）。
- 同步目标之外的会话（其他群/其他私聊）不镜像、不注入。

## 命令

```
/qbridge              状态（连接/目标/机器人号/白名单/待应答）
/qbridge on|off       启停（写回插件配置，重启保持）
/qbridge bind <private|group>/<id>   更改同步目标
```

## 配置项

| key | 默认 | 说明 |
|---|---|---|
| enabled | false | 装载即启动（/qbridge on\|off 同源） |
| ws_url | ws://127.0.0.1:3001 | OneBot 正向 WS 地址 |
| access_token | （空） | 可选鉴权令牌（Authorization: Bearer） |
| self_id | （空） | 机器人 QQ 号，可自动获取 |
| target | （空） | `private/<qq>` / `group/<gid>`，空则自动绑定 |
| allow_users | （空） | 逗号分隔 QQ 号；空=只读镜像 |
| confirm_via_qq | true | 工具确认映射到 QQ |
| sync_tools | true | 工具活动行同步 |
| sync_system | true | 系统消息同步 |
| max_len | 2000 | 单条消息长度上限（分段发送） |

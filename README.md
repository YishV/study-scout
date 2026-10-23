# Study Scout

> 公开点赞、收藏、评论及自动回复可能触发 B 站限流或封禁。只使用专用小号。默认 `DRY_RUN=true`。真实互动必须先完成目标账号通知验收。

独立 Python 3.12 项目。每次在终端启动都会显示紧凑的 B 站登录二维码；扫码并验证登录态之后，才执行一轮筛选。默认每轮最多 5 条。另有本机只读 Web 展示页；无后台刷视频、定时任务或自动续刷。第一版仅接 B 站，抖音只保留 adapter 接口。

## 安装与配置

在 PowerShell 中运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
Copy-Item .env.example .env
```

直接编辑项目根目录的 `.env`。所有配置项在 [.env.example](.env.example) 中都有中文注释；文件中的值优先于同名环境变量，修改后重新启动即可生效。`MODEL_BASE_URL` 填 OpenAI 兼容 API 的基础地址，不附加 `/chat/completions`。Cookie 不写进配置文件，每次需要访问 B 站时重新扫码，只保存在本次进程内存中。`.env` 和 `data/` 已被 Git 忽略。

- `STUDY_TOPICS` 是喜欢看的主题词，英文逗号分隔。程序分别搜索这些主题，交错挑选候选视频，再让模型根据兴趣和字幕筛选；不再先从泛推荐流凑满 5 条。
- `STUDY_PERSONA` 是自由填写的一行人设说明。模型据此判断视频、生成公开评论草稿、回复自己评论下的新回复。字幕和他人回复仍视为不可信内容。
- `BILI_SHARE_METHOD=private_message`（默认）：发表不带 @ 的人设评论，随后给 `BILI_OWNER_UID` 私信视频链接和看点。`BILI_SHARE_METHOD=comment_mention`：在人设评论中 @ 配置的大号，不发私信。两种方式只选一种；私信失败不会改发公开 @。
- `DRY_RUN=true` 时只在本地记录评论与分享草稿，不执行点赞、收藏、评论、私信或回复。

新安装默认保持 `DRY_RUN=true`。本地专用小号的验证评论 RPID `321508476416` 已由目标账号确认收到站内 @ 通知；启用真实互动仍需明确将 `.env` 中的 `DRY_RUN` 改为 `false`。其他账号或目标 UID 应重新验收。

## 启动与停止

```powershell
.\scripts\start.ps1
```

终端显示二维码和临时登录链接。扫码前只等待登录；二维码过期或等待超时会直接退出，不会运行任务。登录成功后执行一轮，打印结果并退出。运行期间按 `Ctrl+C` 停止。没有后台进程，也不再需要 `stop.ps1`。

## 本机展示页

在另一个 PowerShell 终端运行：

```powershell
.\scripts\start.ps1 dashboard
```

终端会给出 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)；保持该终端开启时，用本机浏览器打开链接。页面显示轮次统计、程序检查过的视频、入选理由、跳过原因、点赞/收藏/评论/私信/自动回复的记录和失败原因。点击“刷新数据”读取最新本地记录；按 `Ctrl+C` 关闭页面服务。运行一轮仍在另一个终端执行 `start.ps1`，不会因打开页面而自动运行。

页面只监听 `127.0.0.1`，只读取本地 SQLite；没有配置表单、扫码、写操作或 B 站请求，也不展示模型密钥和 Cookie。“检查的视频”指程序读取过视频详情，并不代表真人播放过；历史数据中原先未记录的跳过原因无法补回。评论和私信的“接口已接受”不等于公开可见或实际送达。

同一时刻只允许一个扫码或运行实例；第二个终端会直接退出。如果升级时旧版 Web 服务仍在运行，启动脚本会根据旧 `data/server.pid` 拒绝启动，并提示核对进程后停止旧服务。

## 终端命令

下面的命令都通过 `start.ps1` 调用。`status`、`dashboard` 和通知确认命令无需扫码；其余命令每次都重新扫码。

```powershell
.\scripts\start.ps1 status
.\scripts\start.ps1 check-replies
```

`check-replies` 只检查程序记录的自己一级评论；在 `DRY_RUN=true` 或此前的真实操作门禁未确认时不会发回复。每条评论最多自动回复三次，回复遵循 `STUDY_PERSONA`。默认单轮在真实操作模式下结束时也检查一次；不进行后台轮询。

如果将来要重新进行 @ 通知验收，先在 `.env` 配好准确的目标 UID 和昵称，然后**主动执行**：

```powershell
.\scripts\start.ps1 verify-mention BVxxxxxxxxxx
.\scripts\start.ps1 status
.\scripts\start.ps1 confirm-notification 123
# 若目标账号未收到站内 @ 通知，改用：
.\scripts\start.ps1 notification-missing 123
```

上面的 `123` 是示例，替换为 `status` 显示的实际评论 ID。

`verify-mention` 会真实发送一条公开测试评论，即使 `DRY_RUN=true` 也一样。只有接口返回的 `members` 包含目标 UID，且你在目标账号确认收到站内 @ 通知后，才用 `confirm-notification` 解锁。未收到时用 `notification-missing` 保持锁定，不自动重发；若通知后来送达，可对同一评论执行 `confirm-notification`。真实模式还需手动将 `.env` 中的 `DRY_RUN` 改为 `false`。写操作之间随机等待 5～20 秒；遇到 412、-509、验证码或频率限制立即停止本轮，不自动重试。

私信发送使用 B 站网页客户端的接口。当前只有离线 fake 验证，尚未用本账号验收私信实际到达。先配置大号 UID，保持 `DRY_RUN=true`，主动执行一次：

```powershell
.\scripts\start.ps1 verify-private-message
```

扫码后此命令**仍会真实发送一条测试私信**，但不会点赞、收藏或评论。请用大号确认私信实际收到，再自行决定是否将 `DRY_RUN` 改为 `false`；接口接受消息不等于送达。评论接口返回成功也不等于评论对其他账号公开可见，需由其他账号检查。

## 离线测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp=.pytest_tmp
```

测试只使用进程内 fake B 站服务和临时 SQLite，不读取项目 `.env`、不使用真实 Cookie。接口记录和 @ 验证状态见 [BILI_API.md](docs/BILI_API.md)。

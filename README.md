# Study Scout

本地运行的 B 站学习内容筛选助手：按主题搜索视频，结合标题、简介和字幕让模型评分，入选后点赞、收藏、评论、@ 或私信分享，并自动回复大号的私信和 @。

> 公开点赞、收藏、评论和自动回复可能触发 B 站限流或封禁，请只使用专用小号。新安装默认 `DRY_RUN=true`。

## 功能

- 按 `STUDY_TOPICS` 逐词搜索并交错挑选候选，不再依赖泛推荐流。
- 模型按人设评估实用、好玩、启发、审美和放松等看点；有字幕的视频至少 70 分才入选。
- 点赞、收藏由模型独立建议，并分别达到 `BILI_LIKE_MIN_SCORE`、`BILI_FAVORITE_MIN_SCORE`。
- 分享方式可选：只私信、评论 @ 大号、评论 @ 并私信。
- 评论后匿名回查公开可见性；回查不到时停止后续评论，避免继续无效写入。
- 监听大号私信、小号评论下的新回复，以及 **大号在视频评论中对小号的 @**。
- 本机页面展示轮次、评分、跳过原因、互动结果和评论可见性。
- 终端与滚动文件日志记录完整运行过程。

## 环境

- Python 3.12
- Windows PowerShell（`scripts/start.ps1`）
- 可访问 B 站和 OpenAI 兼容模型接口

## 安装

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
Copy-Item .env.example .env
```

## 配置

编辑项目根目录的 `.env`，各项含义见 [.env.example](.env.example)。文件中的值优先于同名环境变量，修改后重启生效。

必须配置：

| 配置 | 说明 |
|---|---|
| `BILI_OWNER_UID` | 接收分享的大号 UID |
| `BILI_OWNER_NAME` | 大号准确昵称，评论 @ 模式需要 |
| `MODEL_BASE_URL` | OpenAI 兼容接口基础地址，不带 `/chat/completions` |
| `MODEL_API_KEY` / `MODEL_NAME` | 模型密钥和模型名 |

常用开关：

- `BILI_SHARE_METHOD`：`private_message`（默认，只私信）、`comment_mention`（只评论 @）、`both`（评论 @ 并私信）。
- `DRY_RUN`：默认 `true`，只生成草稿，不执行写操作。
- `BILI_REQUIRE_NOTIFICATION_CONFIRMATION`：默认 `true`，需先确认大号收到测试评论的 @ 通知才允许真实写入。
- `BILI_LISTENER_MAX_AGE_HOURS`：只处理最近多少小时内的回复和 @，默认 24。

Cookie 不写入配置文件：每次启动重新扫码，只保存在当前进程内存。

## 运行

```powershell
.\scripts\start.ps1
```

1. 终端显示二维码和临时登录链接，用专用小号扫码。
2. 登录后立即筛选一轮，之后每 `BILI_RUN_INTERVAL_MINUTES` 分钟一轮。
3. 轮次之间按 `BILI_LISTEN_INTERVAL_SECONDS` 检查私信和通知。
4. 网页地址：<http://127.0.0.1:8765/>。
5. 按 `Ctrl+C` 停止整个服务。

程序启动时只记录当前消息位置；启动前已经收到的私信、回复和 @ 不会补回复，需要时在大号侧重新发一次。

只运行一轮：

```powershell
.\scripts\start.ps1 run
```

只监听消息、不主动刷视频：

```powershell
.\scripts\start.ps1 listen
```

只读展示页（不扫码、不提供手动按钮，与常驻服务共用 8765 端口）：

```powershell
.\scripts\start.ps1 dashboard
```

其他命令：

```powershell
.\scripts\start.ps1 status
.\scripts\start.ps1 check-replies
.\scripts\start.ps1 verify-mention BVxxxxxxxxxx
.\scripts\start.ps1 confirm-notification 123
.\scripts\start.ps1 notification-missing 123
.\scripts\start.ps1 verify-private-message
```

`verify-mention` 和 `verify-private-message` 会真实发送测试内容，即使 `DRY_RUN=true`。`verify-mention` 还会匿名回查这条测试评论；匿名接口看不到时命令返回非零，可直接判断小号是否被限制。单实例运行：同一时刻只允许一个扫码或运行进程。

首次真实写入前建议先做通知验收：

1. 执行 `verify-mention BVxxxxxxxxxx`，用大号确认站内 @ 通知。
2. 用 `status` 查到评论 ID，执行 `confirm-notification <评论ID>`。
3. 未收到通知时执行 `notification-missing <评论ID>`，保持写入锁定。

`BILI_REQUIRE_NOTIFICATION_CONFIRMATION=false` 可以跳过验收，但不代表评论或私信能被对方看到。

## 评论可见性

B 站会对低等级、低信任账号的评论做静默限制，账号行为触发风控时也会出现：接口返回成功和 RPID，但未登录用户看不到。

程序的处理：

1. 发表评论后用不带登录 Cookie 的接口按 RPID 回查。
2. 回查成功记为“匿名公开接口已查到”。
3. 回查不到记为“疑似仅自己可见”，并暂停本次运行后续的自动评论；点赞、收藏和私信不受影响。
4. 网页顶部和终端日志会明确提示。

如果持续出现不可见，先提升账号等级（答题转正、绑定手机），或把 `BILI_SHARE_METHOD` 改为 `private_message`，用私信完成分享。

## 日志

- 终端：按级别输出启动配置、登录账号、每轮进度、判断结果、写操作、回查结果和错误。
- 文件：`data/study-scout.log`，单文件 2 MB，保留 3 个备份。
- `412`、`-509`、验证码或频率限制会立即停止自动轮次，不自动重试。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp=.pytest_tmp
```

测试只使用进程内 fake B 站服务和临时 SQLite，不读取项目 `.env`，不使用真实 Cookie。

## 文档

- [docs/DESIGN.md](docs/DESIGN.md)：模块、数据流和状态机
- [docs/BILI_API.md](docs/BILI_API.md)：使用的接口、通知来源和验收方法

## 已知限制

- 评论是否公开由 B 站风控决定，程序只能检测并停止，不能保证通过。
- 视频内容只依据标题、简介和字幕；不下载视频，不做语音转写或画面分析。
- 抖音仅保留 adapter 接口，未实现。
- 自动回复每次登录有总条数上限，达到上限后需重新启动。

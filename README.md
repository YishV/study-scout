# Study Scout

> 公开点赞、收藏、评论及自动回复可能触发 B 站限流或封禁。只使用专用小号。首次默认 `DRY_RUN=true`，真实互动会被锁住，直到测试评论的 @ 目标 UID 在接口返回中出现，且你在目标账号上亲自确认收到通知。

独立 Python 3.12 项目。启动后本地控制台要求扫码登录；扫码成功自动运行一轮，不定时、不续刷。第一版仅接 B 站。抖音只保留接口占位。

## 安装

在 PowerShell 中运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
Copy-Item .env.example .env
```

编辑 `.env` 或在本地控制台设置主题、目标 UID/准确昵称、模型 OpenAI 兼容 API 地址、模型名和密钥。Cookie 不写入 `.env`，每次启动重新扫码。

```powershell
.\scripts\start.ps1
.\scripts\stop.ps1
```

控制台只监听 `127.0.0.1:8765`。登录后自动跑一轮；“现在跑一轮”是手动再跑。检查回复也由按钮或本轮结束时执行一次，不做后台定时轮询。

## 首次真实互动验收

1. 保持 `DRY_RUN=true`，离线测试通过后，在控制台扫码并确认发现、字幕、判断与评论草稿。
2. 配好目标 UID 和准确昵称。在专用测试视频上点“发验证评论”。这一步会真实发送**一条**公开评论，须由你主动点击；不会点赞或收藏。
3. 控制台显示接口 `members` 包含目标 UID 后，到目标账号确认收到 @ 通知，再点“我已在目标账号收到通知”。未确认前真实动作仍锁住。
4. 将运行模式改为真实操作。此时才允许按“点赞 → 收藏 → 评论”执行，每次写操作间随机等待 5～20 秒。出现 412、-509、验证码或频率限制立即停止本轮，不自动重试。
5. 如普通文本 @ 没有形成真实 mention，控制台标记 `mention_unverified`，不解锁真实动作。可在评论里附目标主页链接作为手动降级，但本版不会自动采用该方案。

有过接口返回目标 UID、评论可见，但目标账号未收到站内 @ 通知的情况。此时保持 `mention_unverified` 与 `DRY_RUN=true`，不自动重发或改用主页链接降级。详情见 [接口记录](docs/BILI_API.md)。

评论回复只检查本程序记录的一级评论，每条最多自动回复三次。对广告、辱骂、敏感或引战内容跳过。网络结果不确定的动作保持记录，不自动重试。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp=.pytest_tmp
```

测试只使用本地 fake B 站 ASGI 服务和临时 SQLite，不读取 `.env`、不使用真实 Cookie。`data/`、`.env` 被 Git 忽略。接口细节和验证状态见 [BILI_API.md](docs/BILI_API.md)。

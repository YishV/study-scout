# 设计说明

本文描述当前实现的模块边界、数据流和关键策略。安装与使用见 [README](../README.md)。

## 1. 目标与范围

Study Scout 是一个单进程、前台运行的 B 站内容筛选助手：

- 按兴趣词搜索视频，读取详情和字幕。
- 调用 OpenAI 兼容模型判断是否值得学习，并生成评论和分享文案。
- 按配置执行点赞、收藏、评论 @ 和私信。
- 监听大号私信、小号评论下的回复，以及大号在视频评论中的 @。
- 提供本地历史页面和手动触发按钮（页面本身不直接调用 B 站）。

不在范围内：浏览器自动化、多账号矩阵、代理池、绕过验证码、视频下载或语音转写、抖音实现。

## 2. 运行形态

```text
scripts/start.ps1
  └─ python -m study_scout.main [serve|run|listen|...]
       ├─ 初始化 SQLite 与日志
       ├─ 控制台显示二维码，扫码后 Cookie 只留在内存
       ├─ serve: 本机 Web 页面 + 调度器
       │    ├─ 登录后先检查消息
       │    ├─ 首轮视频筛选在 N 分钟后
       │    ├─ 之后每 N 分钟一轮
       │    └─ 轮次之间每 60 秒检查消息
       ├─ run: 只跑一轮视频筛选
       └─ listen: 只监听消息
```

页面只监听 `127.0.0.1:8765`，使用标准库 `http.server`；常驻服务与 `dashboard` 命令不能同时占用同一端口。

## 3. 模块

| 模块 | 职责 |
|---|---|
| `config.py` | 读取 `.env`，校验取值范围 |
| `bili/auth.py` | 扫码登录，Cookie 只保存在进程内存 |
| `bili/wbi.py` | WBI 签名 |
| `bili/client.py` | HTTP 客户端、账号信息、通知、评论、点赞、收藏、私信 |
| `bili/comments.py` | 评论构造、@ 成员校验、安全过滤 |
| `judge.py` | 模型评分，输出结构化 `Judgment` |
| `runner.py` | 视频筛选、判断、写操作、限速和熔断 |
| `listener.py` | 私信、回复和 @ 通知处理 |
| `service.py` | 轮次调度、手动触发、下次运行时间 |
| `dashboard.py` | 本机展示页面和手动按钮 |
| `storage.py` | SQLite 表结构与查询 |
| `main.py` | 命令行入口、日志、扫码流程 |

## 4. 视频筛选流程

```text
discover(interests)
  → 逐个兴趣搜索，交错去重
  → 跳过已操作过的 bvid
  → detail(bvid) + subtitles(aid, cid)
  → judge(标题 + 简介 + 字幕)
  → score >= 70 且有看点才入选
  → 有字幕: like / favorite / comment / private_message
  → 无字幕: 仅私信，文案注明未核对画面
  → 评论后匿名回查可见性
```

写操作顺序固定，动作先以 `pending` 落库再发请求，失败不重试，避免网络歧义导致重复写入。每条视频每类动作只有一条记录（`UNIQUE(candidate_id, kind)`）。

## 5. 消息监听

数据来源：

```text
私信: https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs
@ 通知: /x/msgfeed/at
回复通知: /x/msgfeed/reply
```

B 站的「@我的」列表走 `/x/msgfeed/at`，顶层评论里的 @ 会出现在这里；`/x/msgfeed/reply` 处理「回复我的」，回复正文里带的 @ 用 `item.at_details` 标出。程序两个来源都轮询，并用 `(subject_id, source_id)` 作为去重键，保证同一条 @ 只回复一次。

处理规则：

| 通知 | 条件 | 动作 |
|---|---|---|
| 大号 @ 小号（at feed） | 发送者是大号，`business_id=1` | 在 `source_id` 下公开回复 |
| 回复里 @ 小号（reply feed） | `at_details` 含小号 UID，发送者是大号 | 在 `source_id` 下公开回复 |
| 小号评论下的新回复 | `root_id` 是小号评论，`source_id != root_id` | 在原评论下回复 |
| 其他 | 非大号、非小号评论线程、超出时限 | 只推进游标 |

去重与限额：

- 通知游标 `listener_at_cursor_<uid>`、`listener_reply_cursor_<uid>` 持久化，重启不重复处理。
- 首次启动只记录当前位置，不回复历史消息。
- `BILI_LISTENER_MAX_AGE_HOURS` 之外的通知只推进游标，不生成回复。
- 每条评论最多回复 `BILI_COMMENT_MAX_REPLIES` 次。
- 每轮视频最多发表 `BILI_COMMENT_MAX_PER_ROUND` 条公开评论，默认 1 条；滚动 24 小时最多 `BILI_COMMENT_MAX_PER_DAY` 条，默认 3 条；其余视频只点赞、收藏和私信。
- 每次登录最多自动回复 `BILI_LISTEN_MAX_PER_SESSION` 条。
- 发送前先写 `dm_replies` / `mention_replies` / `replies` 占位，失败也不重发。

私信回复只使用最近 6 条上下文；最近分享视频的字幕记忆保存在 `video_memory`，默认 2 小时，按小号与大号隔离。

## 6. 评论可见性

评论接口返回成功只代表请求被接受。程序随后使用独立的匿名 HTTP 客户端：

1. 先查 `/x/v2/reply/reply?root=<rpid>`，返回的根评论 RPID 匹配即公开可见。
2. 返回 `12006` 直接判定匿名不可见；返回 `0` 但未带根评论时，再查 `/x/v2/reply/main` 最新页兜底。
3. `-352` 等风控错误视为“本次无法确认”，不判定为可见。

结果写入 `comments.visibility_status`：

| 值 | 含义 | 后续处理 |
|---|---|---|
| `visible` | 匿名接口已查到 | 继续 |
| `unconfirmed` | 匿名接口未查到 | 暂停本次进程后续评论 |
| `unchecked` | 未回查或回查不可用 | 仅记录 |

B 站对低等级/低信任账号的评论会静默隐藏，无法通过接口规避。程序通过账号等级预检、匿名回查和停止后续评论，避免把“接口成功”误报为“他人可见”。

大号的 @ 回复发出后，程序会按 `root` 线程回查这条回复；不可见时自动改发私信给大号，保证回复内容能送达。私信也失败时只记录，不重试。

## 7. 存储

```text
runs              轮次状态和计数
candidates        读取过的视频
judgments         模型评分和理由
actions           点赞、收藏、评论、私信、草稿
comments          小号评论、@ 校验、通知确认、可见性
replies           小号评论下的自动回复
dm_replies        私信回复去重
mention_replies   @ 通知回复去重
video_memory      最近分享视频的字幕上下文
settings          消息游标等键值
accounts          预留
```

数据库位于 `data/study-scout.sqlite3`。页面使用只读连接读取，不直接发写请求。

## 8. 风控与失败处理

- 两次写操作之间随机等待 `BILI_WRITE_DELAY_MIN`～`BILI_WRITE_DELAY_MAX` 秒，视频轮次和消息回复共用同一个 `WriteGate`。
- 写操作遇到 `412`、`-509`、验证码、频率限制、`-352` 等错误时立即停止本轮，不自动重试。
- 单轮最多处理 5 条视频；每轮消息回复有上限；每次登录有总上限。
- 真实写入前可要求大号确认收到测试评论的 @ 通知。
- 评论未形成真实 @ 时停止本轮，不降级为伪 @。
- 评论匿名回查确认不可见时，停止后续评论，但保留点赞、收藏和私信；回查接口本身失败时只记录，不停止。

## 9. 配置

全部配置在 `.env`，按组划分：

```text
兴趣与账号   STUDY_TOPICS, STUDY_PERSONA, BILI_OWNER_UID, BILI_OWNER_NAME, BILI_SHARE_METHOD
互动门槛     BILI_LIKE_MIN_SCORE, BILI_FAVORITE_MIN_SCORE, BILI_FAV_FOLDER_ID
运行节奏     BILI_BATCH_LIMIT, BILI_RUN_INTERVAL_MINUTES, BILI_COMMENT_MAX_REPLIES,
             BILI_COMMENT_MAX_PER_ROUND, BILI_COMMENT_MAX_PER_DAY,
             BILI_LISTEN_INTERVAL_SECONDS, BILI_LISTEN_MAX_PER_CYCLE,
             BILI_LISTEN_MAX_PER_SESSION, BILI_LISTENER_MAX_AGE_HOURS,
             BILI_VIDEO_MEMORY_HOURS, BILI_WRITE_DELAY_MIN/MAX
模型         MODEL_BASE_URL, MODEL_API_KEY, MODEL_NAME
安全开关     DRY_RUN, BILI_REQUIRE_NOTIFICATION_CONFIRMATION
```

取值范围在 `Settings.from_env()` 中强制校验，越界直接报错，不静默降级。

## 10. 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp=.pytest_tmp
```

测试覆盖：扫码登录、视频筛选、模型判断、写操作顺序、熔断、私信、回复、@ 通知、评论可见性、页面渲染、单实例锁、配置校验。全部使用进程内 fake B 站服务和临时数据库。

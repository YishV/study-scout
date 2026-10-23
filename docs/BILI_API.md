# B 站接口记录

以下 Web 接口用于本项目；接口可能变化，自动化测试只使用 fake 服务，线上验证必须使用专用小号。

## 使用的接口

| 操作 | 路径 | 关键参数 |
|---|---|---|
| 扫码 | `/x/passport-login/web/qrcode/generate`、`/poll` | `qrcode_key` |
| 登录状态 / WBI 密钥 | `/x/web-interface/nav` | Cookie |
| 设备标识 | `/x/frontend/finger/spi` | 无；返回 `b_3`、`b_4` |
| 按兴趣搜索 | `/x/web-interface/wbi/search/type` | 兴趣词、WBI 签名 |
| 视频详情 | `/x/web-interface/view` | `bvid` 或 `aid` |
| 字幕 | `/x/player/wbi/v2` | `aid`、`cid`、WBI 签名 |
| 点赞 | `/x/web-interface/archive/like` | `aid`、`like=1`、`csrf` |
| 收藏 | `/x/v3/fav/resource/deal` | `rid=aid`、`type=2`、收藏夹 ID、`csrf` |
| 评论 / 回复 | `/x/v2/reply/add` | `oid=aid`、`type=1`、`message`、`root`、`parent`、`csrf` |
| 私信发送 | `https://api.vc.bilibili.com/web_im/v1/web_im/send_msg` | 发件/收件 UID、设备 ID、WBI 签名、`csrf` |
| 读取私信 | `https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs` | 会话 UID、`session_type=1`、WBI 签名 |
| 回复通知 | `/x/msgfeed/reply` | 游标 `id`、`reply_time` |
| @ 通知 | `/x/msgfeed/at` | 游标 `id`、`at_time` |
| 评论公开回查 | `/x/v2/reply/reply`、`/x/v2/reply/main` | `oid`、`root` / `mode=2` |

写请求使用浏览器 User-Agent，并带上 `buvid3`、`buvid4` 和 `Origin`，降低被识别为脚本的概率；这不改变 B 站的风控判定。

## 通知

B 站消息中心使用两个来源：

- `/x/msgfeed/at`：**顶层评论里的 @**，即「@我的」列表。
- `/x/msgfeed/reply`：回复我的；回复正文里带的 @ 用 `item.at_details` 标出。

两个接口的 `item` 字段一致：`business_id`（`1` 为视频评论，`11` 为动态）、`subject_id`（视频 aid）、`root_id`、`source_id`（@ 或回复所在的评论）、`source_content`、`at_details`。

程序两个接口都轮询，并只用 `(subject_id, source_id)` 去重；只在发送者是配置的大号时公开回复。只轮询其中一个会漏掉顶层评论 @ 或回复里的 @。

## 评论可见性

评论接口成功只代表请求被接受。匿名回查：

- `/x/v2/reply/reply?root=<rpid>` 返回 `code=0` 且根评论 RPID 匹配：公开可见。
- 返回 `12006`：匿名接口看不到，可能是审核、隐藏或删除。
- 返回 `0` 但未带根评论：再查 `/x/v2/reply/main` 最新页兜底。
- 返回 `-352`：风控拦截，本次无法判断。
- `/x/v2/reply/main` 最新页仅作兜底；新版评论通常不在热门页。

B 站对低等级、低信任账号有静默评论限制。程序在回查不到时暂停后续评论，并在页面和日志中提示。

## 验收

真实写入前至少确认：

1. 扫码账号不是大号本身。
2. `verify-mention` 返回的 `content.members` 包含大号 UID。
3. 大号确实收到站内 @ 通知。
4. 未登录窗口能看到评论；看不到时不要继续批量发表。
5. `verify-private-message` 的测试私信在大号侧实际收到。

Cookie 只保存在进程内存，不写入 `.env` 或数据库。

# B 站接口记录

设计依据是 [DESIGN.md](DESIGN.md) 第 7、10、16 节。以下 Web 接口可能变化；自动化测试只使用 fake 服务，线上验收必须使用专用小号。

| 操作 | 路径 | 必要参数 |
|---|---|---|
| 扫码 | `/x/passport-login/web/qrcode/generate`、`/poll` | `qrcode_key` |
| 登录状态/WBI 密钥 | `/x/web-interface/nav` | Cookie |
| 首页推荐/搜索/热门 | `/x/web-interface/wbi/index/top/feed/rcmd`、`/x/web-interface/wbi/search/type`、`/x/web-interface/popular` | WBI 签名或搜索词 |
| 详情/字幕 | `/x/web-interface/view`、`/x/player/wbi/v2` | bvid / aid、cid |
| 点赞/收藏 | `/x/web-interface/archive/like`、`/x/v3/fav/resource/deal` | aid、收藏夹 ID、csrf |
| 评论/回复 | `/x/v2/reply/add`、`/x/v2/reply/reply` | oid、message、root、parent、csrf |

二维码登录成功后 Cookie 只保存在进程内存，每次启动重新扫码。评论 `@昵称` 是否发出真实通知不能靠文本推断：先检查评论响应中的 `data.reply.content.members` 是否包含目标 UID，再由目标账号人工确认通知。此验证未完成时真实互动锁定。

## 线上验证经验

已有实例中，评论接口 `content.members` 含目标 UID，评论在视频下也可见，但目标账号没有收到站内“回复与@”通知。互关后复验仍是同样结果。系统将两次尝试记为 `mention_unverified` / `notification_missing`，保持 `DRY_RUN=true` 和真实互动锁定。接口返回目标 UID、评论公开可见，都不能替代通知送达确认。具体账号和评论 ID 只保存在本机数据库，不放入公开仓库。

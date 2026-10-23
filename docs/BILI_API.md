# B 站接口记录

设计依据是 [DESIGN.md](DESIGN.md) 第 7、10、16 节。以下 Web 接口可能变化；自动化测试只使用 fake 服务，线上验收必须使用专用小号。

| 操作 | 路径 | 必要参数 |
|---|---|---|
| 扫码 | `/x/passport-login/web/qrcode/generate`、`/poll` | `qrcode_key` |
| 登录状态/WBI 密钥 | `/x/web-interface/nav` | Cookie |
| 按兴趣搜索 | `/x/web-interface/wbi/search/type` | 兴趣词、WBI 签名 |
| 详情/字幕 | `/x/web-interface/view`、`/x/player/wbi/v2` | bvid / aid、cid |
| 点赞/收藏 | `/x/web-interface/archive/like`、`/x/v3/fav/resource/deal` | aid、收藏夹 ID、csrf |
| 评论/回复 | `/x/v2/reply/add`、`/x/v2/reply/reply` | oid、message、root、parent、csrf |
| 私信分享 | `https://api.vc.bilibili.com/web_im/v1/web_im/send_msg` | 发件 UID、目标 UID、文本内容、设备 ID、WBI 签名、登录 Cookie、表单 `csrf=bili_jct` 与 `csrf_token=bili_jct` |

二维码登录成功后 Cookie 只保存在进程内存，每次启动重新扫码。评论 `@昵称` 是否发出真实通知不能靠文本推断：先检查评论响应中的 `data.reply.content.members` 是否包含目标 UID，再由目标账号人工确认通知。此验证未完成时真实互动锁定。私信请求格式参考 B 站网页客户端自身脚本；接口返回成功也需要由收件账号检查实际送达。

## 线上验证经验

已有实例中，评论接口 `content.members` 含目标 UID，目标账号起初没有看到站内“回复与@”通知；后来用户确认通知送达，才解锁真实写入。随后一条评论虽返回成功和 RPID，却只在发件账号下可见，未登录查询返回 `12006`。因此需要分别区分请求成功、@ 通知送达、评论公开可见。私信方式目前只有 fake 服务测试，尚待专用小号和目标大号验收实际到达。

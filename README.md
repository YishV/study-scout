# Study Scout

> 公开点赞、收藏、评论及自动回复可能触发 B 站限流或封禁。只使用专用小号。默认 `DRY_RUN=true`。真实互动必须先验证测试评论确实通知到目标账号；此前保持锁定。

独立 Python 3.12 项目。每次在终端启动都会显示 B 站登录二维码；扫码并验证登录态之后，才执行一轮筛选。默认每轮最多 5 条。无 Web 控制台、后台服务、定时任务或自动续刷。第一版仅接 B 站，抖音只保留 adapter 接口。

## 安装与配置

在 PowerShell 中运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
Copy-Item .env.example .env
```

直接编辑项目根目录的 `.env`，填写主题、目标 UID/准确昵称、模型 API 地址、模型名和密钥。程序启动时读取文件，文件中的值优先于同名环境变量；修改后重新启动即可生效。`MODEL_BASE_URL` 填 OpenAI 兼容 API 的基础地址，不附加 `/chat/completions`。Cookie 不写进配置文件，每次需要访问 B 站时重新扫码，只保存在本次进程内存中。`.env` 和 `data/` 已被 Git 忽略。

先保持 `DRY_RUN=true`。此前测试评论的接口返回包含目标 UID，但目标账号没有收到站内 @ 通知，因此当前数据库里的真实互动锁不能视为已验证。

## 启动与停止

```powershell
.\scripts\start.ps1
```

终端显示二维码和临时登录链接。扫码前只等待登录；二维码过期或等待超时会直接退出，不会运行任务。登录成功后执行一轮，打印结果并退出。运行期间按 `Ctrl+C` 停止。没有后台进程，也不再需要 `stop.ps1`。

同一时刻只允许一个扫码或运行实例；第二个终端会直接退出。如果升级时旧版 Web 服务仍在运行，启动脚本会根据旧 `data/server.pid` 拒绝启动，并提示核对进程后停止旧服务。

## 终端命令

下面的命令都通过 `start.ps1` 调用。`status` 和通知确认命令只读写本地数据库，不需要扫码。其他命令每次都重新扫码。

```powershell
.\scripts\start.ps1 status
.\scripts\start.ps1 check-replies
```

`check-replies` 只检查程序记录的自己一级评论；在 `DRY_RUN=true` 或 @ 通知未确认时不会发回复。每条评论最多自动回复三次。默认单轮在真实操作模式下结束时也检查一次；不进行后台轮询。

如果将来要重新进行 @ 通知验收，先在 `.env` 配好准确的目标 UID 和昵称，然后**主动执行**：

```powershell
.\scripts\start.ps1 verify-mention BVxxxxxxxxxx
.\scripts\start.ps1 status
.\scripts\start.ps1 confirm-notification 123
# 若目标账号未收到站内 @ 通知，改用：
.\scripts\start.ps1 notification-missing 123
```

上面的 `123` 是示例，替换为 `status` 显示的实际评论 ID。

`verify-mention` 会真实发送一条公开测试评论，即使 `DRY_RUN=true` 也一样。只有接口返回的 `members` 包含目标 UID，且你在目标账号确认收到站内 @ 通知后，才用 `confirm-notification` 解锁。未收到时用 `notification-missing` 保持锁定，不自动重发。真实模式还需手动将 `.env` 中的 `DRY_RUN` 改为 `false`。写操作之间随机等待 5～20 秒；遇到 412、-509、验证码或频率限制立即停止本轮，不自动重试。

## 离线测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp=.pytest_tmp
```

测试只使用进程内 fake B 站服务和临时 SQLite，不读取项目 `.env`、不使用真实 Cookie。接口记录和 @ 验证状态见 [BILI_API.md](docs/BILI_API.md)。

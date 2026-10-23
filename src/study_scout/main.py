"""One foreground terminal session: scan, authenticate, then run once."""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import qrcode

from .bili.auth import QRLogin
from .bili.client import BiliClient
from .config import Settings
from .instance_lock import AlreadyRunning, single_instance
from .runner import Runner
from .storage import Store

log = logging.getLogger(__name__)


def render_qr(url: str) -> str:
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    # Pair two module rows per terminal row so long login URLs fit an 80-column console.
    # On a dark terminal, light modules use the foreground and dark modules the background.
    matrix = qr.get_matrix()
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))
    blocks = {(False, False): '█', (False, True): '▀',
              (True, False): '▄', (True, True): ' '}
    return '\n'.join(''.join(blocks[top, bottom] for top, bottom in zip(matrix[i], matrix[i + 1]))
                     for i in range(0, len(matrix), 2))


async def run_command(command, settings, store, *, bvid=None, comment_id=None, bili_http=None,
                      model_http=None, judge=None, sleep=asyncio.sleep, output=print):
    options = dict(bvid=bvid, comment_id=comment_id, bili_http=bili_http,
                   model_http=model_http, judge=judge, sleep=sleep, output=output)
    if command == 'status':
        return await _run_command(command, settings, store, **options)
    try:
        with single_instance(store.path.parent / 'study-scout.lock'):
            return await _run_command(command, settings, store, **options)
    except AlreadyRunning as exc:
        output(str(exc))
        return 1


async def _run_command(command, settings, store, *, bvid=None, comment_id=None, bili_http=None,
                       model_http=None, judge=None, sleep=asyncio.sleep, output=print):
    if command == 'status':
        for run in store.recent_runs():
            output(f"本轮 {run['id']}：{run['status']}，候选 {run['candidates_count']}，入选 {run['selected_count']}，错误 {run['error_code'] or '无'}")
        for comment in store.pending_mentions():
            output(f"待确认通知：评论 ID {comment['id']}，RPID {comment['rpid']}，目标 UID {comment['mention_uid']}")
        output(f"运行模式：{'DRY_RUN' if settings.dry_run else '真实操作'}；@ 通知：{'已确认' if store.mention_ready(settings.owner_uid) else '未确认，真实互动锁定'}")
        return 0

    if command in ('confirm-notification', 'notification-missing'):
        if comment_id is None:
            raise ValueError('请提供待确认的评论 ID；先运行 status 查看')
        changed = (store.set_notification_confirmed(comment_id) if command == 'confirm-notification'
                   else store.mark_notification_missing(comment_id))
        if not changed:
            output('评论不存在或不处于待确认状态；真实互动保持锁定。')
            return 1
        output('通知已确认。' if command == 'confirm-notification' else '未收到通知，真实互动保持锁定。')
        return 0

    if command not in ('run', 'verify-mention', 'check-replies'):
        raise ValueError(f'未知命令：{command}')
    if command == 'verify-mention' and not bvid:
        raise ValueError('请提供测试视频 BV 号；此命令会发一条公开验证评论')
    if command in ('run', 'check-replies') and not settings.dry_run and not store.mention_ready(settings.owner_uid):
        output('真实互动锁定：目标账号尚未确认收到 @ 通知；未开始扫码或运行。')
        return 1
    if command == 'run' and judge is None and not all((settings.model_base_url, settings.model_name, settings.model_api_key)):
        output('请先在 .env 配置 MODEL_BASE_URL、MODEL_NAME、MODEL_API_KEY；未开始扫码或运行。')
        return 1
    if command == 'check-replies' and settings.dry_run:
        output('DRY_RUN：自动回复锁定，未登录或发送。')
        return 0

    async with AsyncExitStack() as stack:
        http = bili_http or await stack.enter_async_context(httpx.AsyncClient(base_url='https://api.bilibili.com', timeout=20))
        model = model_http or await stack.enter_async_context(httpx.AsyncClient(timeout=60))
        bili = BiliClient(http)
        auth = QRLogin(http, bili)
        url = await auth.generate()
        output('请用 B 站专用小号扫码登录。登录成功前不会执行本轮。')
        output(render_qr(url))
        output(f'若终端二维码无法识别，可打开临时登录链接：{url}')
        for _ in range(90):
            state = await auth.poll()
            if state == 'logged_in':
                break
            if state == 'expired':
                output('二维码已过期，未运行任何任务；请重新启动。')
                return 1
            await sleep(2)
        else:
            output('等待扫码超时，未运行任何任务；请重新启动。')
            return 1

        output('登录验证通过。')
        runner = Runner(settings, store, bili, judge=judge, sleep=sleep, model_http=model)
        if command == 'verify-mention':
            result = await runner.verify_test_mention(bvid)
            output(f"公开验证评论 RPID {result['rpid']}；接口成员匹配：{result['reply_member_verified']}。")
            output('请在目标账号站内查看 @ 通知，再用 status 查评论 ID 并手动确认。')
            return 0 if result['reply_member_verified'] else 1
        if command == 'check-replies':
            sent = await runner.check_replies()
            output(f'检查完成，自动回复 {sent} 条。')
            return 0

        output('运行模式：' + ('DRY_RUN，只生成草稿。' if settings.dry_run else '真实操作。'))
        run_id = await runner.run_once()
        run = store.recent_runs()[0]
        output(f"本轮 {run_id}：{run['status']}，候选 {run['candidates_count']}，入选 {run['selected_count']}，错误 {run['error_code'] or '无'}")
        for item in store.run_results(run_id):
            output(f"{item['bvid']} {item['title']}｜{item['score'] or '-'}｜{item['kind'] or '-'}：{item['status'] or '-'}")
        return 0 if run['status'] == 'done' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='扫码登录后执行一轮 B 站学习筛选')
    parser.add_argument('command', nargs='?', choices=('run', 'status', 'verify-mention', 'confirm-notification', 'notification-missing', 'check-replies'), default='run')
    parser.add_argument('value', nargs='?', help='验证视频 BV 号或待确认的评论 ID')
    args = parser.parse_args(argv)
    if args.command in ('verify-mention', 'confirm-notification', 'notification-missing') and not args.value:
        parser.error('此命令需要视频 BV 号或评论 ID')
    if args.command in ('run', 'status', 'check-replies') and args.value:
        parser.error('此命令不接受额外参数')
    try:
        settings = Settings.from_env()
        store = Store(Path('data/study-scout.sqlite3'))
        store.init()
        comment_id = int(args.value) if args.command in ('confirm-notification', 'notification-missing') else None
        return asyncio.run(run_command(args.command, settings, store,
                                       bvid=args.value if args.command == 'verify-mention' else None,
                                       comment_id=comment_id))
    except KeyboardInterrupt:
        print('已取消。')
        return 130
    except Exception as exc:
        log.error('命令失败：%s', exc)
        print(f'命令失败：{exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

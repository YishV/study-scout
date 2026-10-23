"""Foreground terminal commands: one video run or an explicit message listener."""

import argparse
import asyncio
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import qrcode
from qrcode.constants import ERROR_CORRECT_L

from .bili.auth import QRLogin
from .bili.client import BiliClient
from .config import Settings
from .dashboard import dashboard_in_background, serve_dashboard
from .instance_lock import AlreadyRunning, single_instance
from .listener import MessageListener
from .runner import Runner, WriteGate
from .service import RunController, StudyService
from .storage import Store

log = logging.getLogger(__name__)


def render_qr(url: str) -> str:
    qr = qrcode.QRCode(border=2, error_correction=ERROR_CORRECT_L)
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
        method = {'private_message': '私信', 'comment_mention': '评论区 @',
                  'both': '评论区 @ + 私信'}[settings.share_method]
        notification = ('已确认' if store.mention_ready(settings.owner_uid) else
                        '未确认，真实互动锁定' if settings.require_notification_confirmation else
                        '未验收，已选择直接互动')
        output(f"运行模式：{'DRY_RUN' if settings.dry_run else '真实操作'}；分享方式：{method}；@ 通知：{notification}")
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

    if command not in ('serve', 'run', 'listen', 'verify-mention', 'verify-private-message', 'check-replies'):
        raise ValueError(f'未知命令：{command}')
    if command == 'verify-mention' and not bvid:
        raise ValueError('请提供测试视频 BV 号；此命令会发一条公开验证评论')
    if command == 'verify-private-message' and not settings.owner_uid:
        raise ValueError('请先在 .env 配置大号 BILI_OWNER_UID')
    if (command in ('serve', 'run', 'listen', 'check-replies') and not settings.dry_run
            and settings.require_notification_confirmation and not store.mention_ready(settings.owner_uid)):
        output('真实互动锁定：目标账号尚未确认收到 @ 通知；未开始扫码或运行。')
        return 1
    needs_model = (command in ('serve', 'run') and judge is None) or (command == 'listen' and not settings.dry_run)
    if needs_model and not all((settings.model_base_url, settings.model_name, settings.model_api_key)):
        output('请先在 .env 配置 MODEL_BASE_URL、MODEL_NAME、MODEL_API_KEY；未开始扫码或运行。')
        return 1
    if command == 'listen' and not settings.owner_uid:
        output('请先在 .env 配置大号 BILI_OWNER_UID；未开始扫码或监听。')
        return 1
    if command == 'check-replies' and settings.dry_run:
        output('DRY_RUN：自动回复锁定，未登录或发送。')
        return 0

    async with AsyncExitStack() as stack:
        http = bili_http or await stack.enter_async_context(httpx.AsyncClient(base_url='https://api.bilibili.com', timeout=20))
        model = model_http or await stack.enter_async_context(httpx.AsyncClient(timeout=60))
        bili = BiliClient(http)
        auth = QRLogin(http, bili)
        while True:
            url = await auth.generate()
            output('请用 B 站专用小号扫码登录。登录成功前不会运行。')
            output(render_qr(url))
            output(f'若终端二维码无法识别，可打开临时登录链接：{url}')
            for _ in range(90):
                state = await auth.poll()
                if state in ('logged_in', 'expired'):
                    break
                await sleep(2)
            else:
                state = 'timeout'
            if state == 'logged_in':
                break
            output('二维码已过期，正在刷新。' if state == 'expired' else '等待扫码超时，正在刷新二维码。')
            await sleep(2)

        account_uid = int((await bili.nav())['mid'])
        if account_uid == settings.owner_uid:
            output('扫码的是配置的大号，请改用专用小号；未运行任何任务。')
            return 1
        output(f'登录验证通过，小号 UID：{account_uid}。')
        if command == 'verify-private-message':
            sender_uid = (await bili.nav())['mid']
            await bili.send_private_message(sender_uid, settings.owner_uid,
                                            'Study Scout 私信分享验证：请确认是否收到这条来自专用小号的测试私信。')
            output('私信接口已接受一条测试消息；请在目标账号检查实际送达。未发送点赞、收藏或评论。')
            return 0
        write_gate = WriteGate(settings, sleep=sleep) if command == 'serve' else None
        runner = Runner(settings, store, bili, judge=judge, sleep=sleep,
                        model_http=model, write_gate=write_gate)
        if command == 'serve':
            controller = RunController(settings)
            listener = (MessageListener(settings, store, bili, model_http=model,
                                        sleep=sleep, output=output, write_gate=write_gate)
                        if settings.owner_uid else None)
            output('运行模式：' + ('DRY_RUN，只生成草稿。' if settings.dry_run else '真实互动。'))
            with dashboard_in_background(store, controller, output=output):
                service = StudyService(settings, store, runner, listener, controller, output=output)
                await service.run_forever()
            return 0 if controller.snapshot()['last_status'] in (None, 'done') else 1
        if command == 'listen':
            listener = MessageListener(settings, store, bili, model_http=model, sleep=sleep, output=output)
            output('运行模式：' + ('DRY_RUN，只读取新消息。' if settings.dry_run else '真实自动回复。'))
            await listener.run_forever()
            return 0
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
            if settings.dry_run and item['kind'] == 'draft':
                draft = json.loads(item['request_json'] or '{}')
                output(f"  评论草稿：{draft.get('message', '')}")
                if draft.get('share_message'):
                    output(f"  私信草稿：{draft['share_message'].replace(chr(10), '｜')}")
        return 0 if run['status'] == 'done' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='扫码登录后持续运行，定时刷视频并监听新消息')
    parser.add_argument('command', nargs='?', choices=('serve', 'run', 'listen', 'status', 'dashboard', 'verify-mention', 'verify-private-message', 'confirm-notification', 'notification-missing', 'check-replies'), default='serve')
    parser.add_argument('value', nargs='?', help='验证视频 BV 号或待确认的评论 ID')
    args = parser.parse_args(argv)
    if args.command in ('verify-mention', 'confirm-notification', 'notification-missing') and not args.value:
        parser.error('此命令需要视频 BV 号或评论 ID')
    if args.command in ('serve', 'run', 'listen', 'status', 'dashboard', 'verify-private-message', 'check-replies') and args.value:
        parser.error('此命令不接受额外参数')
    try:
        store = Store(Path('data/study-scout.sqlite3'))
        store.init()
        if args.command == 'dashboard':
            serve_dashboard(store)
            return 0
        settings = Settings.from_env()
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

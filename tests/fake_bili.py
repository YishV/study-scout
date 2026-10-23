"""In-process Bilibili HTTP double. Tests never use a live account."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def make_fake_bili(*, fail_code: int = 0, dm_fail_code: int = 0, mention_uid: int = 42, subtitles: bool = True, poll_codes: tuple[int, ...] = (0,), public_comment_visible: bool = True, public_comment_in_latest: bool = True, public_reply_visible: bool = False, public_fail_code: int = 0):
    app = FastAPI()
    calls: list[tuple[str, dict]] = []
    poll_index = 0
    dm_messages: list[dict] = []
    reply_notifications: list[dict] = []
    at_notifications: list[dict] = []

    @app.api_route('/{path:path}', methods=['GET', 'POST'])
    async def handle(path: str, request: Request):
        nonlocal poll_index
        form = dict(await request.form()) if request.method == 'POST' else dict(request.query_params)
        if request.method == 'POST':
            form.update(request.query_params)
        if path.endswith('/send_msg'):
            form['_has_cookie'] = bool(request.headers.get('cookie'))
            csrf = request.cookies.get('bili_jct')
            if not csrf or form.get('csrf') != csrf or form.get('csrf_token') != csrf:
                calls.append((path, form))
                return {'code': -111, 'message': 'CSRF 校验失败'}
        calls.append((path, form))
        if path.endswith('qrcode/generate'):
            return {'code': 0, 'data': {'qrcode_key': 'fake-key', 'url': 'https://fake.test/login'}}
        if path.endswith('qrcode/poll'):
            code = poll_codes[min(poll_index, len(poll_codes) - 1)]
            poll_index += 1
            response = JSONResponse({'code': 0, 'data': {'code': code}})
            if code == 0:
                response.set_cookie('SESSDATA', 'fake-session')
                response.set_cookie('bili_jct', 'fake-csrf')
                response.set_cookie('DedeUserID', '99')
            return response
        if fail_code and request.method == 'POST':
            return {'code': fail_code, 'message': 'risk control'}
        if dm_fail_code and path.endswith('/send_msg'):
            return {'code': dm_fail_code, 'message': '私信发送受限'}
        if path == 'x/web-interface/nav':
            return {'code': 0, 'data': {'mid': 99, 'isLogin': True, 'wbi_img': {'img_url': 'https://i/7cd084941338484aae1ad9425b84077c.png', 'sub_url': 'https://i/4932caff0ff746eab6f01bf08b70ac45.png'}}}
        if path == 'x/frontend/finger/spi':
            return {'code': 0, 'data': {'b_3': 'fake-buvid3', 'b_4': 'fake-buvid4'}}
        if path == 'svr_sync/v1/svr_sync/fetch_session_msgs':
            return {'code': 0, 'data': {'messages': sorted(dm_messages, key=lambda m: m['msg_seqno'], reverse=True)[:50],
                                        'has_more': int(len(dm_messages) > 50)}}
        if path in ('x/msgfeed/reply', 'x/msgfeed/at'):
            offset = int(form.get('id') or 0)
            source = reply_notifications if path.endswith('/reply') else at_notifications
            items = sorted(source, key=lambda m: int(m['id']), reverse=True)
            if offset:
                items = [item for item in items if int(item['id']) < offset]
            page = items[:20]
            return {'code': 0, 'data': {'items': page,
                                        'cursor': {'id': page[-1]['id'] if page else 0,
                                                   'time': (page[-1].get('reply_time') or page[-1].get('at_time')) if page else 0,
                                                   'is_end': len(items) <= 20}}}
        if path.endswith('feed/rcmd') or path.endswith('popular') or path.endswith('search/type'):
            return {'code': 0, 'data': {'item': [{'bvid': 'BV1test', 'title': 'Python 调试', 'owner': {'mid': 7, 'name': '老师'}}], 'list': [{'bvid': 'BV1test', 'title': 'Python 调试'}], 'result': []}}
        if path == 'x/web-interface/view':
            if (form.get('bvid') not in (None, 'BV1test') or form.get('aid') not in (None, '1')):
                return {'code': -404, 'message': '视频不存在'}
            return {'code': 0, 'data': {'bvid': 'BV1test', 'aid': 1, 'cid': 2, 'title': 'Python 调试', 'desc': '最小复现', 'owner': {'mid': 7, 'name': '老师'}, 'duration': 600, 'pubdate': 1}}
        if path == 'x/player/wbi/v2':
            return {'code': 0, 'data': {'subtitle': {'subtitles': [{'subtitle_url': '//fake.test/subtitle.json'}] if subtitles else []}}}
        if path == 'subtitle.json':
            return {'body': [{'content': '先最小复现，再定位问题。'}]}
        if path == 'x/v3/fav/folder/created/list-all':
            return {'code': 0, 'data': {'list': [{'id': 9, 'title': '学习'}]}}
        if path == 'x/v2/reply/reply':
            form['_has_cookie'] = bool(request.headers.get('cookie'))
            if public_fail_code:
                return {'code': public_fail_code, 'message': 'risk control'}
            if form.get('root') == '201':
                return ({'code': 0, 'data': {'root': {'rpid': 201}, 'replies': []}}
                        if public_comment_visible else {'code': 12006, 'message': '没有该评论'})
            return {'code': 0, 'data': {'replies': [{'rpid': 401 if public_reply_visible else 301,
                                                      'member': {'mid': 8},
                                                      'content': {'message': '怎么复现？'}}]}}
        if path == 'x/v2/reply/main':
            form['_has_cookie'] = bool(request.headers.get('cookie'))
            if public_fail_code:
                return {'code': public_fail_code, 'message': 'risk control'}
            return {'code': 0, 'data': {'replies': [{'rpid': 201}] if public_comment_visible and public_comment_in_latest else []}}
        if path == 'x/v2/reply/add':
            return {'code': 0, 'data': {'reply': {'rpid': 201 if 'root' not in form else 401, 'content': {'members': [{'mid': mention_uid}] if mention_uid else []}}}}
        return {'code': 0, 'data': {}}

    app.state.dm_messages = dm_messages
    app.state.reply_notifications = reply_notifications
    app.state.at_notifications = at_notifications
    return app, calls

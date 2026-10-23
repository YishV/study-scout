"""Local, read-only dashboard for completed and in-progress runs."""

from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit


_CHINA_TIME = timezone(timedelta(hours=8))
_RUN_STATUS = {'running': '运行中', 'done': '已结束', 'failed': '失败',
               'circuit_open': '风控熔断', 'stopped': '已停止'}
_SKIP_REASON = {'duplicate_title': '标题重复，跳过', 'duplicate_author': '作者重复，跳过',
                'no_subtitles': '无字幕，跳过', 'judge_error': '模型判断失败',
                'not_selected': '未达到入选条件', 'invalid_comment': '评论草稿不合规'}
_ACTION = {'like': '点赞', 'favorite': '收藏', 'comment': '评论',
           'private_message': '私信', 'draft': '草稿', 'mention_test': '@ 验证', 'judge': '模型判断'}

_STYLE = """
:root{color-scheme:light;font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif;color:#1b2940;background:#f2f5fa}
*{box-sizing:border-box}body{margin:0}.wrap{max-width:1160px;margin:auto;padding:30px 22px 70px}
header{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:24px}
h1{font-size:30px;letter-spacing:-.04em;margin:0 0 7px}h2{font-size:20px;margin:0 0 18px}h3{font-size:17px;margin:0 0 10px}
p{line-height:1.6;margin:7px 0}.sub,.muted{color:#64748b}.sub{font-size:14px}.muted{font-size:13px}
a{color:#2458b8;text-decoration:none}a:hover{text-decoration:underline}.refresh{border:1px solid #c7d4e8;border-radius:10px;padding:9px 14px;background:white;white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:22px}.stat,.panel,.video{background:#fff;border:1px solid #e1e7f0;border-radius:16px;box-shadow:0 4px 20px rgba(30,50,90,.035)}
.stat{padding:18px}.stat strong{display:block;font-size:28px;margin-top:7px}.stat span{font-size:13px;color:#64748b}
.grid{display:grid;grid-template-columns:270px minmax(0,1fr);gap:18px}.panel{padding:20px}.runs{display:grid;gap:9px}.run{display:block;border:1px solid #e1e7f0;border-radius:11px;padding:12px;color:inherit}.run.active{background:#edf4ff;border-color:#96b9f4}.run:hover{text-decoration:none;background:#f6f9fe}
.run-top{display:flex;justify-content:space-between;gap:8px;font-size:14px;font-weight:700}.run-meta{display:block;color:#64748b;font-size:12px;margin-top:5px}.paging{display:flex;justify-content:space-between;margin-top:15px;font-size:13px}
.video{padding:19px;margin:0 0 14px}.video-head{display:flex;justify-content:space-between;gap:14px}.video-title{font-size:17px;font-weight:700;line-height:1.45}.badge{display:inline-block;font-size:12px;font-weight:650;border-radius:99px;padding:5px 9px;background:#eef3fa;color:#334155;white-space:nowrap}.badge.good{background:#e5f6ec;color:#11633b}.badge.bad{background:#fff0ec;color:#aa3426}.badge.draft{background:#fff4db;color:#8c5d0d}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0}.detail{border-top:1px solid #e9edf4;padding-top:12px;margin-top:12px}.message{background:#f7f9fd;border-radius:9px;padding:10px 12px;margin:8px 0;overflow-wrap:anywhere}.empty{padding:34px 15px;text-align:center;color:#64748b}.note{font-size:13px;color:#65758d;margin-top:16px}.error{color:#aa3426}
@media(max-width:850px){.stats{grid-template-columns:repeat(3,1fr)}.grid{grid-template-columns:1fr}}@media(max-width:540px){.wrap{padding:18px 12px 50px}.stats{grid-template-columns:repeat(2,1fr)}h1{font-size:25px}.video-head{display:block}.video-head .badge{margin-top:8px}}
"""


def _safe(value):
    return escape('' if value is None else str(value), quote=True)


def _time(value):
    if not value:
        return '—'
    try:
        return datetime.fromisoformat(value).astimezone(_CHINA_TIME).strftime('%m-%d %H:%M')
    except ValueError:
        return _safe(value)


def _action_badge(kind, status, error=None):
    label = _ACTION.get(kind, kind)
    if kind == 'draft':
        return '<span class="badge draft">草稿，未发送</span>'
    if status == 'ok':
        suffix = '接口已接受' if kind in ('comment', 'private_message') else '成功'
        return f'<span class="badge good">{_safe(label)}{suffix}</span>'
    if status in ('failed', 'mention_unverified'):
        detail = f'（{_safe(error)}）' if error else ''
        return f'<span class="badge bad">{_safe(label)}失败{detail}</span>'
    return f'<span class="badge">{_safe(label)}：{_safe(status)}</span>'


def _video_card(video):
    bvid = str(video['bvid'] or '')
    url = 'https://www.bilibili.com/video/' + quote(bvid, safe='')
    title = _safe(video['title'] or bvid)
    metadata = f"{_safe(video['author'] or '未知作者')} · {_time(video['seen_at'])} · {_safe(bvid)}"
    if video['skip_reason']:
        state = _SKIP_REASON.get(video['skip_reason'], video['skip_reason'])
        state_class = 'bad' if video['skip_reason'] in ('judge_error', 'invalid_comment') else ''
    elif video['actions'].get('draft') == 'dry_run':
        state, state_class = '入选，只有草稿', 'draft'
    elif any(status == 'failed' for status in video['actions'].values()):
        state, state_class = '互动失败', 'bad'
    elif video['score'] is not None:
        state, state_class = ('已入选' if video['worth_learning'] and video['score'] >= 70 else '未入选'), ''
    else:
        state, state_class = '待判断', ''
    chips = ''.join(_action_badge(kind, status, video['action_errors'].get(kind))
                    for kind, status in video['actions'].items())
    judgment = ''
    if video['score'] is not None:
        judgment = (f'<p><strong>学习分数 {_safe(video["score"])}</strong> · {_safe(video["reason"])}</p>'
                    + (f'<p class="muted">学习点：{_safe("；".join(video["takeaways"]))}</p>'
                       if video['takeaways'] else ''))
    comments = ''
    for comment in video['comments']:
        note = ('@ 通知已确认' if comment['notification_confirmed'] else
                '评论接口已接受；公开可见和通知送达未验收' if comment['status'] == 'ok' else
                '评论状态：' + str(comment['status']))
        replies = ''.join(f'<div class="message">自动回复：{_safe(reply["message"])} '
                          f'<span class="muted">({_safe(reply["status"])})</span></div>'
                          for reply in comment['replies'])
        comments += (f'<div class="detail"><p class="muted">评论 RPID {_safe(comment["rpid"])} · {_safe(note)}</p>'
                     f'<div class="message">{_safe(comment["message"])}</div>{replies}</div>')
    return (f'<article class="video"><div class="video-head"><div><a class="video-title" href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>'
            f'<p class="muted">{metadata}</p></div><span class="badge {state_class}">{_safe(state)}</span></div>'
            f'{judgment}<div class="chips">{chips}</div>{comments}</article>')


def render_dashboard(data):
    """Render escaped HTML from a bounded local SQLite snapshot."""
    totals = data['totals']
    stats = [('已运行轮次', totals['runs']), ('检查的视频', totals['videos']),
             ('入选视频', totals['selected']), ('接口成功', totals['actions_ok']),
             ('互动失败', totals['actions_failed']), ('评论自动回复', totals['replies_ok']),
             ('私信自动回复', totals['dm_replies_ok'])]
    stat_html = ''.join(f'<div class="stat"><span>{label}</span><strong>{_safe(value)}</strong></div>'
                        for label, value in stats)
    selected = data['selected_run']
    run_links = ''.join(
        f'<a class="run {"active" if selected and selected["id"] == run["id"] else ""}" href="/?page={data["page"]}&amp;run={run["id"]}">'
        f'<span class="run-top"><span>第 {run["id"]} 轮</span><span>{_safe(_RUN_STATUS.get(run["status"], run["status"]))}</span></span>'
        f'<span class="run-meta">{_time(run["started_at"])} · 检查 {run["candidates_count"]} · 入选 {run["selected_count"]}</span></a>'
        for run in data['runs']) or '<div class="empty">还没有运行记录</div>'
    previous = f'<a href="/?page={data["page"] - 1}">← 较新</a>' if data['page'] > 1 else '<span></span>'
    following = f'<a href="/?page={data["page"] + 1}">更早 →</a>' if data['has_next'] else '<span></span>'
    video_html = ''.join(_video_card(video) for video in data['videos']) or '<div class="empty">本轮没有已记录的视频</div>'
    heading = f'第 {selected["id"]} 轮的视频' if selected else '视频记录'
    run_note = (f'开始：{_time(selected["started_at"])} · 结束：{_time(selected["finished_at"])}'
                + (f' · 错误：{_safe(selected["error_code"])}' if selected['error_code'] else '')) if selected else ''
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Study Scout · 运行记录</title><style>{_STYLE}</style></head><body><main class="wrap">'
            f'<header><div><h1>Study Scout</h1><p class="sub">本机运行记录 · 只读展示</p></div><a class="refresh" href="/">刷新数据</a></header>'
            f'<section class="stats">{stat_html}</section><div class="grid"><aside class="panel"><h2>运行记录</h2><div class="runs">{run_links}</div>'
            f'<div class="paging">{previous}{following}</div></aside><section class="panel"><h2>{heading}</h2><p class="sub">{run_note}</p>{video_html}'
            f'<p class="note">“接口已接受”仅表示 B 站返回成功；评论公开可见、@ 通知和私信送达需在目标账号核对。刷新页面读取本地数据库，不会访问 B 站或触发互动。</p>'
            f'</section></div></main></body></html>')


def make_handler(store):
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            host = self.headers.get('Host', '')
            if host not in (f'127.0.0.1:{self.server.server_port}',
                            f'localhost:{self.server.server_port}'):
                self.send_error(403)
                return
            parsed = urlsplit(self.path)
            if parsed.path != '/':
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            try:
                page = max(1, min(100000, int(query.get('page', ['1'])[0])))
                run_id = int(query['run'][0]) if 'run' in query else None
            except ValueError:
                self.send_error(400)
                return
            body = render_dashboard(store.dashboard_data(page, run_id)).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    return DashboardHandler


def serve_dashboard(store, *, port=8765, output=print):
    with ThreadingHTTPServer(('127.0.0.1', port), make_handler(store)) as server:
        output(f'只读展示页：http://127.0.0.1:{server.server_port}/')
        output('保持此终端开启；刷新网页可查看新记录，按 Ctrl+C 关闭。')
        server.serve_forever(poll_interval=0.5)

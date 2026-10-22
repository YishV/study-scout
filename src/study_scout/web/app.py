import asyncio
import io
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from ..bili.auth import QRLogin
from ..bili.client import BiliClient
from ..runner import Runner

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))


def create_app(settings, store, *, bili_http=None, model_http=None, env_path=Path('.env')):
    @asynccontextmanager
    async def lifespan(app):
        yield
        app.state.runner.stop()
        if app.state.owned_http:
            await app.state.bili_http.aclose()
        if model_http is None:
            await app.state.model_http.aclose()

    app = FastAPI(title='Study Scout', lifespan=lifespan)
    token = secrets.token_urlsafe(32)
    app.state.form_token = token
    control_path = store.path.parent / 'control.token'
    control_path.write_text(token, encoding='ascii')
    app.state.owned_http = bili_http is None
    app.state.bili_http = bili_http or httpx.AsyncClient(base_url='https://api.bilibili.com', timeout=20)
    app.state.model_http = model_http or httpx.AsyncClient(timeout=60)
    app.state.bili = BiliClient(app.state.bili_http)
    app.state.auth = QRLogin(app.state.bili_http, app.state.bili)
    app.state.runner = Runner(settings, store, app.state.bili, model_http=app.state.model_http)
    app.state.auto_started = False
    app.state.run_task = None

    def guard(value):
        if not secrets.compare_digest(value or '', token):
            raise HTTPException(403, '表单令牌无效')

    def persist_config():
        values = {'STUDY_TOPICS': settings.topics, 'BILI_OWNER_UID': settings.owner_uid, 'BILI_OWNER_NAME': settings.owner_name, 'BILI_FAV_FOLDER_ID': settings.fav_folder_id or '', 'BILI_BATCH_LIMIT': settings.batch_limit, 'BILI_COMMENT_MAX_REPLIES': settings.max_replies, 'BILI_WRITE_DELAY_MIN': settings.write_delay_min, 'BILI_WRITE_DELAY_MAX': settings.write_delay_max, 'MODEL_BASE_URL': settings.model_base_url, 'MODEL_API_KEY': settings.model_api_key, 'MODEL_NAME': settings.model_name, 'DRY_RUN': str(settings.dry_run).lower()}
        for value in values.values():
            if '\n' in str(value) or '\r' in str(value):
                raise HTTPException(400, '配置不能包含换行')
        Path(env_path).write_text(''.join(f'{key}={value}\n' for key, value in values.items()), encoding='utf-8')

    @app.get('/', response_class=HTMLResponse)
    async def index(request: Request):
        runs = store.recent_runs()
        return TEMPLATES.TemplateResponse(request, 'index.html', {'settings': settings, 'runs': runs, 'results': store.run_results(runs[0]['id']) if runs else [], 'comments': store.recent_comments(), 'replies': store.recent_replies(), 'pending_mentions': store.pending_mentions(), 'logged_in': bool(app.state.bili.csrf), 'runner': app.state.runner, 'token': token, 'mention_ready': store.mention_ready(settings.owner_uid)})

    @app.post('/qr')
    async def qr(csrf_token: str = Form('')):
        guard(csrf_token)
        try:
            url = await app.state.auth.generate()
            svg = io.BytesIO()
            qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage).save(svg)
            return Response(svg.getvalue(), media_type='image/svg+xml')
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get('/login-status')
    async def login_status():
        if not app.state.auth.key:
            return {'status': 'logged_in' if app.state.bili.csrf else 'waiting'}
        try:
            status = await app.state.auth.poll()
            if status == 'logged_in' and not app.state.auto_started:
                app.state.auto_started = True
                app.state.run_task = asyncio.create_task(app.state.runner.run_once())
            return {'status': status}
        except Exception as exc:
            return JSONResponse({'status': 'error', 'message': str(exc)}, status_code=502)

    @app.post('/run')
    async def run(csrf_token: str = Form('')):
        guard(csrf_token)
        if not app.state.bili.csrf:
            raise HTTPException(400, '先扫码登录')
        if app.state.runner.lock.locked() or (app.state.run_task and not app.state.run_task.done()):
            raise HTTPException(409, '已经在运行')
        app.state.run_task = asyncio.create_task(app.state.runner.run_once())
        return {'started': True}

    @app.post('/config')
    async def config(csrf_token: str = Form(''), topics: str = Form(''), owner_uid: str = Form(''), owner_name: str = Form(''), fav_folder_id: str = Form(''), model_base_url: str = Form(''), model_name: str = Form(''), model_api_key: str = Form(''), dry_run: str = Form('true')):
        guard(csrf_token)
        try:
            settings.topics = topics.strip()
            settings.owner_uid = int(owner_uid or 0)
            settings.owner_name = owner_name.strip()
            settings.fav_folder_id = int(fav_folder_id or 0)
            settings.model_base_url = model_base_url.strip()
            settings.model_name = model_name.strip()
            if model_api_key:
                settings.model_api_key = model_api_key
            settings.dry_run = dry_run != 'false'
            persist_config()
        except ValueError as exc:
            raise HTTPException(400, f'配置格式无效: {exc}') from exc
        return {'saved': True, 'dry_run': settings.dry_run}

    @app.get('/owner-candidates')
    async def owner_candidates(name: str):
        if not app.state.bili.csrf:
            raise HTTPException(400, '先扫码登录')
        return {'candidates': await app.state.bili.find_users(name.strip())}

    @app.post('/choose-owner')
    async def choose_owner(uid: int = Form(...), name: str = Form(...), csrf_token: str = Form('')):
        guard(csrf_token)
        matches = await app.state.bili.find_users(name.strip())
        if not any(item['uid'] == uid for item in matches):
            raise HTTPException(400, '目标 UID 与昵称未匹配')
        settings.owner_uid, settings.owner_name = uid, name.strip()
        persist_config()
        return {'saved': True, 'uid': uid}

    @app.post('/stop')
    async def stop(csrf_token: str = Form('')):
        guard(csrf_token)
        app.state.runner.stop()
        if app.state.run_task and not app.state.run_task.done() and not app.state.runner.lock.locked():
            app.state.run_task.cancel()
        return {'stopping': True}

    @app.post('/verify-mention')
    async def verify_mention(bvid: str = Form(''), csrf_token: str = Form('')):
        guard(csrf_token)
        if app.state.run_task and not app.state.run_task.done():
            raise HTTPException(409, '运行中不能验证')
        try:
            return await app.state.runner.verify_test_mention(bvid.strip())
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/confirm-notification')
    async def confirm_notification(comment_id: int = Form(...), csrf_token: str = Form('')):
        guard(csrf_token)
        if not store.set_notification_confirmed(comment_id):
            raise HTTPException(400, '该评论的目标 UID 未在接口返回中得到验证')
        return {'unlocked': True}

    @app.post('/notification-missing')
    async def notification_missing(comment_id: int = Form(...), csrf_token: str = Form('')):
        guard(csrf_token)
        if not store.mark_notification_missing(comment_id):
            raise HTTPException(400, '没有待确认的测试评论')
        return {'mention_unverified': True, 'real_actions_locked': True}

    @app.post('/check-replies')
    async def check_replies(csrf_token: str = Form('')):
        guard(csrf_token)
        if app.state.runner.lock.locked():
            raise HTTPException(409, '正在运行')
        async with app.state.runner.lock:
            count = await app.state.runner.check_replies()
        return {'replies_sent': count}

    @app.get('/status')
    async def status():
        return {'logged_in': bool(app.state.bili.csrf), 'step': app.state.runner.step, 'running': app.state.runner.lock.locked() or bool(app.state.run_task and not app.state.run_task.done()), 'runs': store.recent_runs(), 'mention_ready': store.mention_ready(settings.owner_uid)}

    return app

import httpx
import pytest

from fake_bili import make_fake_bili
from study_scout.config import Settings
from study_scout.storage import Store
from study_scout.web.app import create_app


@pytest.mark.asyncio
async def test_web_starts_without_cookie_and_requires_csrf(tmp_path):
    fake, _ = make_fake_bili()
    store = Store(tmp_path / 'test.db')
    store.init()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as bili_http:
        app = create_app(Settings(), store, bili_http=bili_http)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8765') as browser:
            page = await browser.get('/')
            assert page.status_code == 200 and '扫码' in page.text
            assert (await browser.post('/run')).status_code == 403


@pytest.mark.asyncio
async def test_config_update_requires_csrf_and_persists_without_echoing_secret(tmp_path):
    fake, _ = make_fake_bili()
    store = Store(tmp_path / 'test.db')
    store.init()
    settings = Settings()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as bili_http:
        app = create_app(settings, store, bili_http=bili_http, env_path=tmp_path / '.env')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8765') as browser:
            assert (await browser.post('/config', data={'topics': 'Python'})).status_code == 403
            token = app.state.form_token
            result = await browser.post('/config', data={'csrf_token': token, 'topics': 'Python', 'owner_uid': '42', 'owner_name': '示例用户', 'model_api_key': 'secret'})
            assert result.status_code == 200 and 'secret' not in result.text
            assert settings.owner_uid == 42
            assert 'MODEL_API_KEY=secret' in (tmp_path / '.env').read_text(encoding='utf-8')

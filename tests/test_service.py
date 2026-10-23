"""Continuous scheduling and web controls are tested without a real account."""

import asyncio
import http.client
import re
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest
import httpx

from fake_bili import make_fake_bili
from study_scout import main as cli
from study_scout.config import Settings
from study_scout.dashboard import make_handler
from study_scout.service import RunController, StudyService
from study_scout.runner import Runner, WriteGate
from study_scout.storage import Store


class FakeClock:
    def __init__(self):
        self.value = 0
        self.waits = []

    def __call__(self):
        return self.value

    async def wait(self, seconds):
        self.waits.append(seconds)
        self.value += seconds


class FakeRunner:
    def __init__(self, store, controller, *, stop_after=2, status='done'):
        self.store, self.controller = store, controller
        self.stop_after, self.status = stop_after, status
        self.calls = []

    async def run_once(self, *, check_replies=True):
        self.calls.append(check_replies)
        run_id = self.store.start_run()
        self.store.finish_run(run_id, self.status)
        if len(self.calls) >= self.stop_after:
            self.controller.stop()
        return run_id


@pytest.mark.asyncio
async def test_service_runs_immediately_and_again_after_configured_interval(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    settings = Settings(owner_uid=0, dry_run=True, run_interval_minutes=10)
    controller = RunController(settings)
    clock = FakeClock()
    runner = FakeRunner(store, controller)
    service = StudyService(settings, store, runner, None, controller,
                           clock=clock, wait=clock.wait, output=lambda _: None)
    await service.run_forever()
    assert runner.calls == [False, False]
    assert clock.waits == [600]
    assert len(store.recent_runs()) == 2


@pytest.mark.asyncio
async def test_manual_run_starts_early_and_resets_timer(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    settings = Settings(owner_uid=0, dry_run=True, run_interval_minutes=10)
    controller = RunController(settings)
    clock = FakeClock()
    runner = FakeRunner(store, controller, stop_after=10)
    waits = []

    async def request_after_30_seconds(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            clock.value += 30
            assert controller.request_run()
        else:
            controller.stop()

    service = StudyService(settings, store, runner, None, controller,
                           clock=clock, wait=request_after_30_seconds, output=lambda _: None)
    await service.run_forever()
    assert runner.calls == [False, False]
    assert clock.value == 30
    assert waits == [600, 600]
    assert controller.snapshot()['last_run_id'] == 2


@pytest.mark.asyncio
async def test_circuit_open_stops_scheduler_without_retry(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    settings = Settings(owner_uid=0, dry_run=True)
    controller = RunController(settings)
    clock = FakeClock()
    runner = FakeRunner(store, controller, stop_after=10, status='circuit_open')
    service = StudyService(settings, store, runner, None, controller,
                           clock=clock, wait=clock.wait, output=lambda _: None)
    await service.run_forever()
    assert runner.calls == [False]
    assert clock.waits == []
    assert controller.snapshot()['state'] == 'stopped'


def test_web_run_button_requires_local_origin_and_csrf_and_rejects_duplicate(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    controller = RunController(Settings(dry_run=True))
    with ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store, controller)) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            host = f'127.0.0.1:{server.server_port}'
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('GET', '/')
            page = connection.getresponse().read().decode('utf-8')
            token = re.search(r'name="token" value="([^"]+)"', page).group(1)
            assert '手动刷一轮' in page and 'DRY_RUN' in page

            body = urlencode({'token': token})
            connection.request('POST', '/run-once', body=body,
                               headers={'Content-Type': 'application/x-www-form-urlencoded',
                                        'Origin': 'https://evil.example'})
            assert connection.getresponse().status == 403
            connection.request('POST', '/run-once', body=urlencode({'token': 'wrong'}),
                               headers={'Content-Type': 'application/x-www-form-urlencoded',
                                        'Origin': f'http://{host}'})
            assert connection.getresponse().status == 403
            assert not controller.snapshot()['pending']

            headers = {'Content-Type': 'application/x-www-form-urlencoded',
                       'Origin': f'http://{host}'}
            connection.request('POST', '/run-once', body=body, headers=headers)
            assert connection.getresponse().status == 303
            assert controller.snapshot()['pending']
            connection.request('POST', '/run-once', body=body, headers=headers)
            assert connection.getresponse().status == 409
            assert controller.take_pending()
            controller.start_activity('running')
            connection.request('POST', '/run-once', body=body, headers=headers)
            assert connection.getresponse().status == 409
            connection.close()
        finally:
            server.shutdown()
            worker.join(timeout=3)


def test_default_cli_command_is_continuous_service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    commands = []

    async def capture(command, *_args, **_kwargs):
        commands.append(command)
        return 0

    monkeypatch.setattr(cli, 'run_command', capture)
    assert cli.main([]) == 0
    assert commands == ['serve']


def test_run_interval_is_configurable_but_not_faster_than_ten_minutes(tmp_path):
    path = tmp_path / '.env'
    path.write_text('BILI_RUN_INTERVAL_MINUTES=15\n', encoding='utf-8')
    assert Settings.from_env(path).run_interval_minutes == 15
    path.write_text('BILI_RUN_INTERVAL_MINUTES=5\n', encoding='utf-8')
    with pytest.raises(ValueError, match='至少为 10'):
        Settings.from_env(path)


@pytest.mark.asyncio
async def test_video_and_message_writes_share_one_rate_gate(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    waits = []

    async def record(seconds):
        waits.append(seconds)

    settings = Settings()
    gate = WriteGate(settings, sleep=record)
    video = Runner(settings, store, None, write_gate=gate)
    messages = Runner(settings, store, None, write_gate=gate)
    await video._wait_write()
    await messages._wait_write()
    assert len(waits) == 1
    assert 5 <= waits[0] <= 20


@pytest.mark.asyncio
async def test_web_thread_wakes_sleeping_scheduler_without_waiting_ten_minutes():
    controller = RunController(Settings())
    controller.bind_loop()
    waiting = asyncio.create_task(controller.wait(600))
    assert await asyncio.to_thread(controller.request_run)
    await asyncio.wait_for(waiting, timeout=1)
    assert controller.take_pending()


@pytest.mark.asyncio
async def test_serve_scans_qr_then_runs_one_fake_round_with_dashboard(tmp_path, monkeypatch):
    fake, calls = make_fake_bili()
    store = Store(tmp_path / 'test.db')
    store.init()
    opened = []

    @contextmanager
    def fake_dashboard(_store, _controller, *, output):
        opened.append(True)
        yield

    async def one_cycle(self):
        await self.listener.poll_once()
        await self._run_video()
        self.controller.stop()

    async def judge(*_args):
        return {'worth_learning': False, 'score': 20, 'reason': '一般',
                'takeaways': [], 'risk': '无'}

    monkeypatch.setattr(cli, 'dashboard_in_background', fake_dashboard)
    monkeypatch.setattr(StudyService, 'run_forever', one_cycle)
    settings = Settings(owner_uid=42, dry_run=True, model_name='fake')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('serve', settings, store, bili_http=http,
                                       judge=judge, output=lambda _: None)
    paths = [path for path, _ in calls]
    assert result == 0
    assert opened == [True]
    assert paths.index('x/passport-login/web/qrcode/poll') < paths.index('x/web-interface/wbi/search/type')
    assert store.recent_runs()[0]['status'] == 'done'
    assert not any(path in ('x/web-interface/archive/like', 'x/v2/reply/add',
                            'web_im/v1/web_im/send_msg') for path in paths)


@pytest.mark.asyncio
async def test_real_service_keeps_notification_gate_before_qr(tmp_path):
    fake, calls = make_fake_bili()
    store = Store(tmp_path / 'test.db')
    store.init()
    messages = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('serve', Settings(owner_uid=42, dry_run=False), store,
                                       bili_http=http, output=messages.append)
    assert result == 1
    assert not calls
    assert any('锁定' in message for message in messages)

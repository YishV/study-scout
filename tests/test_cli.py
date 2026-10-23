"""The command line tests use only the in-process fake Bilibili service."""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from fake_bili import make_fake_bili
from study_scout.config import Settings
from study_scout import main as cli
from study_scout.instance_lock import AlreadyRunning, single_instance
from study_scout.storage import Store


def fake_judge(*_):
    return asyncio.sleep(0, result={
        'worth_learning': True,
        'score': 80,
        'reason': '具体',
        'takeaways': ['先复现'],
        'risk': '无',
    })


def test_login_qr_fits_a_normal_terminal():
    url = 'https://passport.bilibili.com/h5-app/passport/login/scan?navhide=1&qrcode_key=' + 'x' * 72
    lines = cli.render_qr(url).splitlines()
    assert lines
    assert max(map(len, lines)) <= 80


def test_login_qr_is_compact_but_keeps_a_readable_fallback_link():
    url = 'https://passport.bilibili.com/h5-app/passport/login/scan?navhide=1&qrcode_key=' + 'x' * 72
    lines = cli.render_qr(url).splitlines()
    assert len(lines[0]) <= 49
    assert len(lines) <= 25


def test_dashboard_command_starts_without_scanning_or_running(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opened = []
    monkeypatch.setattr(cli, 'serve_dashboard', lambda store: opened.append(store.path), raising=False)

    assert cli.main(['dashboard']) == 0
    assert opened == [Path('data/study-scout.sqlite3')]
    assert Store(opened[0]).recent_runs() == []


def test_session_lock_is_shared_between_processes(tmp_path):
    lock_path = tmp_path / 'study-scout.lock'
    ready_path = tmp_path / 'ready'
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from study_scout.instance_lock import single_instance\n"
        "with single_instance(Path(sys.argv[2])):\n"
        "    Path(sys.argv[3]).write_text('ready')\n"
        "    sys.stdin.readline()\n"
    )
    source = Path(__file__).resolve().parents[1] / 'src'
    child = subprocess.Popen([sys.executable, '-c', code, str(source), str(lock_path), str(ready_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            if ready_path.exists() or child.poll() is not None:
                break
            time.sleep(0.05)
        assert ready_path.exists(), f'child return code: {child.poll()}'
        with pytest.raises(AlreadyRunning):
            with single_instance(lock_path):
                pass
    finally:
        child.communicate(input='\n', timeout=5)
    assert child.returncode == 0
    with single_instance(lock_path):
        pass


@pytest.mark.asyncio
async def test_start_prints_qr_and_waits_for_login_before_one_round(tmp_path):
    fake, calls = make_fake_bili(poll_codes=(86101, 86090, 0))
    store = Store(tmp_path / 'test.db')
    store.init()
    output = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('run', Settings(topics='Python', owner_uid=42, owner_name='示例用户', model_name='fake'), store, bili_http=http, judge=fake_judge, sleep=lambda _: asyncio.sleep(0), output=output.append)
    paths = [path for path, _ in calls]
    assert result == 0
    assert 'qrcode/generate' in paths[0]
    assert paths.count('x/passport-login/web/qrcode/poll') == 3
    assert paths.index('x/passport-login/web/qrcode/poll') < paths.index('x/web-interface/wbi/search/type')
    assert store.recent_runs()[0]['status'] == 'done'
    assert len(store.recent_runs()) == 1
    assert any('扫码' in line for line in output)
    assert any('DRY_RUN' in line for line in output)
    assert any('评论草稿：先复现' in line for line in output)
    assert any('私信草稿：' in line and 'BV1test' in line for line in output)
    assert not any(path in ('x/web-interface/archive/like', 'x/v3/fav/resource/deal', 'x/v2/reply/add') for path in paths)


@pytest.mark.asyncio
async def test_expired_qr_refreshes_and_only_starts_after_login(tmp_path):
    fake, calls = make_fake_bili(poll_codes=(86038, 0))
    store = Store(tmp_path / 'test.db')
    store.init()
    messages = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('run', Settings(), store, bili_http=http, judge=fake_judge, sleep=lambda _: asyncio.sleep(0), output=messages.append)
    assert result == 0
    assert store.recent_runs()[0]['status'] == 'done'
    assert len([path for path, _ in calls if path.endswith('qrcode/generate')]) == 2
    assert len([path for path, _ in calls if path.endswith('qrcode/poll')]) == 2
    assert any('过期' in message for message in messages)
    assert [path for path, _ in calls if path.endswith('search/type')]


@pytest.mark.asyncio
async def test_real_mode_can_start_after_scan_when_confirmation_is_optional(tmp_path):
    fake, calls = make_fake_bili()
    store = Store(tmp_path / 'test.db')
    store.init()
    settings = Settings(topics='Python', owner_uid=42, owner_name='示例用户',
                        fav_folder_id=9, dry_run=False, max_replies=0,
                        require_notification_confirmation=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('run', settings, store, bili_http=http,
                                       judge=fake_judge, sleep=lambda _: asyncio.sleep(0), output=lambda _: None)
    assert result == 0
    assert not store.mention_ready(42)
    paths = [path for path, _ in calls]
    assert paths.index('x/passport-login/web/qrcode/poll') < paths.index('x/web-interface/archive/like')
    assert 'x/v3/fav/resource/deal' in paths
    assert 'x/v2/reply/add' in paths
    assert 'web_im/v1/web_im/send_msg' in paths


@pytest.mark.asyncio
async def test_scanning_owner_account_stops_before_any_round(tmp_path):
    fake, calls = make_fake_bili()
    store = Store(tmp_path / 'test.db')
    store.init()
    settings = Settings(owner_uid=99, dry_run=False,
                        require_notification_confirmation=False)
    messages = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('run', settings, store, bili_http=http,
                                       judge=fake_judge, output=messages.append)
    assert result == 1
    assert not store.recent_runs()
    assert any('大号' in message for message in messages)
    assert not any(path.endswith('feed/rcmd') for path, _ in calls)


@pytest.mark.asyncio
async def test_unverified_real_mode_stops_before_login_or_network(tmp_path):
    fake, calls = make_fake_bili()
    store = Store(tmp_path / 'test.db')
    store.init()
    messages = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('run', Settings(owner_uid=42, dry_run=False), store, bili_http=http, output=messages.append)
    assert result == 1
    assert not calls
    assert not store.recent_runs()
    assert any('锁定' in message for message in messages)


@pytest.mark.asyncio
async def test_second_terminal_cannot_start_while_first_waits_for_scan(tmp_path):
    fake, calls = make_fake_bili(poll_codes=(86101, 0))
    store = Store(tmp_path / 'test.db')
    store.init()
    settings = Settings(topics='Python', owner_uid=42, owner_name='示例用户', model_name='fake')
    first_waiting = asyncio.Event()
    continue_first = asyncio.Event()

    async def hold_first(_):
        first_waiting.set()
        await continue_first.wait()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        first = asyncio.create_task(cli.run_command('run', settings, store, bili_http=http, judge=fake_judge, sleep=hold_first, output=lambda _: None))
        await asyncio.wait_for(first_waiting.wait(), 2)
        calls_before_second = len(calls)
        messages = []
        try:
            second = await cli.run_command('run', settings, store, bili_http=http, judge=fake_judge, sleep=lambda _: asyncio.sleep(0), output=messages.append)
            assert second == 1
            assert len(calls) == calls_before_second
            assert any('已有' in message for message in messages)
        finally:
            continue_first.set()
            assert await first == 0
    assert len(store.recent_runs()) == 1


@pytest.mark.asyncio
async def test_missing_model_config_stops_before_qr(tmp_path):
    fake, calls = make_fake_bili()
    store = Store(tmp_path / 'test.db')
    store.init()
    messages = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('run', Settings(), store, bili_http=http, output=messages.append)
    assert result == 1
    assert not calls
    assert not store.recent_runs()
    assert any('MODEL_' in message for message in messages)


@pytest.mark.asyncio
async def test_notification_confirmation_command_never_calls_bili(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    run = store.start_run()
    candidate = store.add_candidate(run, {'bvid': 'BVtest', 'aid': 1, 'cid': 2, 'title': 'test'})
    comment_id = store.save_comment(candidate, 321, '@示例用户 test', 42, True, 'pending_confirmation')
    fake, calls = make_fake_bili()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('confirm-notification', Settings(owner_uid=42), store, comment_id=comment_id, bili_http=http, output=lambda _: None)
    assert result == 0
    assert store.mention_ready(42)
    assert not calls


@pytest.mark.asyncio
async def test_verify_mention_is_explicit_and_does_not_unlock_writes(tmp_path):
    fake, calls = make_fake_bili()
    store = Store(tmp_path / 'test.db')
    store.init()
    settings = Settings(owner_uid=42, owner_name='示例用户', dry_run=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('verify-mention', settings, store, bvid='BV1test', bili_http=http, sleep=lambda _: asyncio.sleep(0), output=lambda _: None)
    assert result == 0
    assert [path for path, _ in calls if path == 'x/v2/reply/add'] == ['x/v2/reply/add']
    assert not any(path in ('x/web-interface/archive/like', 'x/v3/fav/resource/deal') for path, _ in calls)
    assert store.pending_mentions()
    assert not store.mention_ready(42)


@pytest.mark.asyncio
async def test_verify_private_message_only_sends_one_test_dm(tmp_path):
    fake, calls = make_fake_bili()
    store = Store(tmp_path / 'test.db')
    store.init()
    settings = Settings(owner_uid=42, dry_run=True)
    messages = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        result = await cli.run_command('verify-private-message', settings, store, bili_http=http,
                                       sleep=lambda _: asyncio.sleep(0), output=messages.append)
    assert result == 0
    assert [path for path, _ in calls if path.endswith('/send_msg')] == ['web_im/v1/web_im/send_msg']
    assert not any(path in ('x/web-interface/archive/like', 'x/v3/fav/resource/deal', 'x/v2/reply/add')
                   for path, _ in calls)
    assert any('目标账号检查' in line for line in messages)


def test_env_file_is_the_editable_config_source(tmp_path, monkeypatch):
    monkeypatch.setenv('STUDY_TOPICS', 'stale-parent-value')
    path = tmp_path / '.env'
    path.write_text('STUDY_TOPICS=Python\nMODEL_NAME=deepseek-chat\nDRY_RUN=true\n', encoding='utf-8')
    settings = Settings.from_env(path)
    assert settings.topics == 'Python'
    assert settings.model_name == 'deepseek-chat'
    assert settings.dry_run is True


def test_notification_confirmation_can_be_disabled_in_local_config(tmp_path):
    path = tmp_path / '.env'
    path.write_text('DRY_RUN=false\nBILI_REQUIRE_NOTIFICATION_CONFIRMATION=false\n', encoding='utf-8')
    settings = Settings.from_env(path)
    assert settings.dry_run is False
    assert settings.require_notification_confirmation is False


def test_persona_interests_and_share_method_come_from_env(tmp_path):
    path = tmp_path / '.env'
    path.write_text(
        '# 喜欢看的内容\n'
        'STUDY_TOPICS=Python, AI ,软件工程\n'
        '# 发评和回复的人设\n'
        'STUDY_PERSONA=认真、简短、只谈具体做法\n'
        '# 视频分享方式\n'
        'BILI_SHARE_METHOD=private_message\n', encoding='utf-8'
    )
    settings = Settings.from_env(path)
    assert settings.interests == ['Python', 'AI', '软件工程']
    assert settings.persona == '认真、简短、只谈具体做法'
    assert settings.share_method == 'private_message'


def test_unknown_share_method_is_rejected(tmp_path):
    path = tmp_path / '.env'
    path.write_text('BILI_SHARE_METHOD=auto_fallback\n', encoding='utf-8')
    with pytest.raises(ValueError, match='BILI_SHARE_METHOD'):
        Settings.from_env(path)

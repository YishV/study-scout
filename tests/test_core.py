import asyncio
import json

import httpx
import pytest

from fake_bili import make_fake_bili
from study_scout.config import Settings
from study_scout.storage import Store
from study_scout.bili.client import BiliClient, BiliError
from study_scout.bili.auth import QRLogin
from study_scout.bili.wbi import sign_params
from study_scout.judge import parse_judgment
from study_scout.bili.comments import build_comment, verified_member
from study_scout.runner import Runner


def test_defaults_and_storage(tmp_path):
    settings = Settings()
    assert settings.dry_run is True and settings.batch_limit == 5
    store = Store(tmp_path / 'test.db')
    store.init()
    run = store.start_run()
    candidate = store.add_candidate(run, {'bvid': 'BV1test', 'aid': 1, 'cid': 2, 'title': 'test'})
    assert store.add_candidate(run, {'bvid': 'BV1test', 'aid': 1, 'cid': 2, 'title': 'test'}) == candidate
    assert store.record_action(candidate, 'like', 'ok')
    assert not store.record_action(candidate, 'like', 'ok')


def test_reply_record_can_only_be_claimed_once(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    run = store.start_run()
    candidate = store.add_candidate(run, {'bvid': 'BVtest', 'aid': 1, 'cid': 2, 'title': 'test'})
    comment = store.save_comment(candidate, 55, '@示例用户 test', 42, True, 'ok')
    assert store.save_reply(comment, 301, None, '第一次', 'pending') is True
    assert store.save_reply(comment, 301, None, '第二次', 'pending') is False
    assert store.reply_count(comment) == 1


def test_missing_notification_cannot_be_confirmed_later(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    run = store.start_run()
    candidate = store.add_candidate(run, {'bvid': 'BVtest', 'aid': 1, 'cid': 2, 'title': 'test'})
    comment = store.save_comment(candidate, 321, '@示例用户 test', 42, True, 'pending_confirmation')
    assert store.mark_notification_missing(comment)
    assert not store.set_notification_confirmed(comment)
    assert not store.mention_ready(42)
    assert store.recent_runs()[0]['error_code'] == 'notification_missing'


def test_wbi_signature_is_stable():
    params = sign_params({'foo': "a!'()", 'z': 2}, '7cd084941338484aae1ad9425b84077c', '4932caff0ff746eab6f01bf08b70ac45', now=1700000000)
    assert params['foo'] == 'a'
    assert params['wts'] == 1700000000
    assert len(params['w_rid']) == 32
    assert params == sign_params({'z': 2, 'foo': "a!'()"}, '7cd084941338484aae1ad9425b84077c', '4932caff0ff746eab6f01bf08b70ac45', now=1700000000)


@pytest.mark.asyncio
async def test_qr_login_cookie_only_in_memory():
    app, _ = make_fake_bili()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://fake.test') as http:
        bili = BiliClient(http)
        qr = QRLogin(http, bili)
        assert await qr.generate() == 'https://fake.test/login'
        assert await qr.poll() == 'logged_in'
        assert bili.csrf == 'fake-csrf'


def test_judge_and_comment_validation():
    result = parse_judgment(json.dumps({'worth_learning': True, 'score': 86, 'reason': '具体方法', 'takeaways': ['先复现'], 'risk': '无'}))
    assert result.score == 86
    assert build_comment('示例用户', result.takeaways[0]) == '@示例用户 先复现'
    with pytest.raises(ValueError):
        parse_judgment('不是 JSON')
    with pytest.raises(ValueError):
        build_comment('示例用户', 'https://example.com')
    assert verified_member({'data': {'reply': {'content': {'members': [{'mid': 42}]}}}}, 42)
    assert not verified_member({'data': {'reply': {'content': {'members': []}}}}, 42)


@pytest.mark.asyncio
async def test_fake_service_and_risk_control():
    app, calls = make_fake_bili(fail_code=-509)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://fake.test') as http:
        bili = BiliClient(http, cookie='SESSDATA=x; bili_jct=csrf; DedeUserID=99')
        assert (await bili.nav())['mid'] == 99
        with pytest.raises(BiliError) as e:
            await bili.like(1)
        assert e.value.stop_run
    assert calls[-1][0] == 'x/web-interface/archive/like'


@pytest.mark.asyncio
async def test_runner_dry_run_never_writes(tmp_path):
    app, calls = make_fake_bili()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://fake.test') as http:
        store = Store(tmp_path / 'test.db')
        store.init()
        runner = Runner(Settings(topics='Python', owner_uid=42, owner_name='示例用户', model_name='fake', dry_run=True), store, BiliClient(http), judge=lambda *a: asyncio.sleep(0, result={'worth_learning': True, 'score': 80, 'reason': '具体', 'takeaways': ['先复现'], 'risk': '无'}), sleep=lambda _: asyncio.sleep(0))
        await runner.run_once()
    assert not any(p in ('x/web-interface/archive/like', 'x/v3/fav/resource/deal', 'x/v2/reply/add') for p, _ in calls)


@pytest.mark.asyncio
async def test_missing_subtitle_skips_judgment_and_actions(tmp_path):
    app, calls = make_fake_bili(subtitles=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://fake.test') as http:
        store = Store(tmp_path / 'test.db')
        store.init()
        runner = Runner(Settings(), store, BiliClient(http), judge=lambda *a: pytest.fail('judge should not run'))
        await runner.run_once()
        assert store.recent_runs()[0]['selected_count'] == 0
        assert not any(p == 'x/v2/reply/add' for p, _ in calls)


@pytest.mark.asyncio
async def test_real_actions_locked_until_notification_confirmed(tmp_path):
    app, calls = make_fake_bili()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://fake.test') as http:
        store = Store(tmp_path / 'test.db')
        store.init()
        settings = Settings(topics='Python', owner_uid=42, owner_name='示例用户', dry_run=False)
        runner = Runner(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99'))
        await runner.run_once()
        assert store.recent_runs()[0]['status'] == 'failed'
        assert not any(method in ('x/web-interface/archive/like', 'x/v2/reply/add') for method, _ in calls)


@pytest.mark.asyncio
async def test_mention_verification_needs_member_and_human_confirmation(tmp_path):
    app, calls = make_fake_bili(mention_uid=0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://fake.test') as http:
        store = Store(tmp_path / 'test.db')
        store.init()
        runner = Runner(Settings(owner_uid=42, owner_name='示例用户'), store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99'), sleep=lambda _: asyncio.sleep(0))
        result = await runner.verify_test_mention('BV1test')
        assert not result['reply_member_verified']
        assert not store.mention_ready()
        assert not store.set_notification_confirmed(result['comment_id'])
    assert [p for p, _ in calls if p == 'x/v2/reply/add'] == ['x/v2/reply/add']


@pytest.mark.asyncio
async def test_confirmed_mention_allows_real_actions_once_and_509_stops(tmp_path):
    app, calls = make_fake_bili()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://fake.test') as http:
        store = Store(tmp_path / 'test.db')
        store.init()
        settings = Settings(topics='Python', owner_uid=42, owner_name='示例用户', fav_folder_id=9, dry_run=False)
        bili = BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99')
        runner = Runner(settings, store, bili, judge=lambda *a: asyncio.sleep(0, result={'worth_learning': True, 'score': 80, 'reason': '具体', 'takeaways': ['先复现'], 'risk': '无'}), sleep=lambda _: asyncio.sleep(0))
        verification = await runner.verify_test_mention('BV1test')
        assert verification['reply_member_verified']
        assert not store.mention_ready()
        assert store.set_notification_confirmed(verification['comment_id'])
        assert store.mention_ready()
        await runner.run_once()
        assert not any(p == 'x/web-interface/archive/like' for p, _ in calls)  # test video is never reused


@pytest.mark.asyncio
async def test_412_circuit_breaker_stops_before_next_action(tmp_path):
    app, calls = make_fake_bili(fail_code=412)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://fake.test') as http:
        store = Store(tmp_path / 'test.db')
        store.init()
        run = store.start_run()
        candidate = store.add_candidate(run, {'bvid': 'BVother', 'aid': 11, 'cid': 12, 'title': 'old'})
        comment = store.save_comment(candidate, 55, '@示例用户 test', 42, True, 'pending_confirmation')
        store.set_notification_confirmed(comment)
        settings = Settings(topics='Python', owner_uid=42, owner_name='示例用户', fav_folder_id=9, dry_run=False)
        runner = Runner(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99'), judge=lambda *a: asyncio.sleep(0, result={'worth_learning': True, 'score': 80, 'reason': '具体', 'takeaways': ['先复现'], 'risk': '无'}), sleep=lambda _: asyncio.sleep(0))
        await runner.run_once()
        assert store.recent_runs()[0]['status'] == 'circuit_open'
        assert [p for p, _ in calls if p in ('x/web-interface/archive/like', 'x/v3/fav/resource/deal', 'x/v2/reply/add')] == ['x/web-interface/archive/like']


@pytest.mark.asyncio
async def test_action_order_and_reply_limit_with_fake_service(tmp_path):
    app, calls = make_fake_bili()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://fake.test') as http:
        store = Store(tmp_path / 'test.db')
        store.init()
        run = store.start_run()
        candidate = store.add_candidate(run, {'bvid': 'BVverify', 'aid': 11, 'cid': 12, 'title': 'old'})
        comment = store.save_comment(candidate, 55, '@示例用户 test', 42, True, 'pending_confirmation')
        store.set_notification_confirmed(comment)
        settings = Settings(topics='Python', owner_uid=42, owner_name='示例用户', fav_folder_id=9, max_replies=1, dry_run=False)
        runner = Runner(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99'), judge=lambda *a: asyncio.sleep(0, result={'worth_learning': True, 'score': 80, 'reason': '具体', 'takeaways': ['先复现'], 'risk': '无'}), reply=lambda *a: asyncio.sleep(0, result='谢谢，你可以先试试最小复现。'), sleep=lambda _: asyncio.sleep(0))
        await runner.run_once()
        writes = [p for p, _ in calls if p in ('x/web-interface/archive/like', 'x/v3/fav/resource/deal', 'x/v2/reply/add')]
        assert writes[:3] == ['x/web-interface/archive/like', 'x/v3/fav/resource/deal', 'x/v2/reply/add']
        assert store.reply_count(comment) == 1
        count = len(writes)
        await runner.check_replies()
        assert len([p for p, _ in calls if p in ('x/web-interface/archive/like', 'x/v3/fav/resource/deal', 'x/v2/reply/add')]) == count
        assert store.seen_bvid('BV1test')

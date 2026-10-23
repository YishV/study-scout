"""Foreground message listening uses only the in-process Bilibili fake."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fake_bili import make_fake_bili
from study_scout.bili.client import BiliClient, BiliError
from study_scout.config import Settings
from study_scout import main as cli
from study_scout.listener import MessageListener
from study_scout.storage import Store


def dm(seq, uid=42, body='你好', status=0):
    return {'msg_seqno': seq, 'msg_key': seq * 10, 'sender_uid': uid,
            'msg_type': 1, 'msg_status': status,
            'content': json.dumps({'content': body}, ensure_ascii=False)}


def notice(ident, root=55, uid=8, body='怎么做？'):
    return {'id': ident, 'reply_time': ident, 'user': {'mid': uid},
            'item': {'business_id': 1, 'subject_id': 11, 'root_id': root,
                     'source_id': ident + 100, 'source_content': body}}


def at_notice(ident, uid=42, body='@小号 这个视频讲得怎么样？', aid=1, mentioned=99):
    # B 站把视频评论里的 @ 放进 /x/msgfeed/reply，用 at_details 标出被 @ 的账号。
    return {'id': ident, 'reply_time': ident, 'user': {'mid': uid},
            'item': {'business_id': 1, 'subject_id': aid, 'root_id': 0,
                     'source_id': ident + 200, 'source_content': body,
                     'at_details': [{'mid': mentioned}] if mentioned else []}}


def ready_store(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    run = store.start_run()
    candidate = store.add_candidate(run, {'bvid': 'BVold', 'aid': 11, 'cid': 12, 'title': '旧视频'})
    comment = store.save_comment(candidate, 55, '原评论', 42, True, 'pending_confirmation')
    assert store.set_notification_confirmed(comment)
    return store, comment


@pytest.mark.asyncio
async def test_listener_baselines_history_then_replies_once_to_owner_dm_and_own_comment(tmp_path):
    fake, calls = make_fake_bili()
    fake.state.dm_messages.extend([dm(10), dm(11, uid=77)])
    fake.state.reply_notifications.append(notice(10))
    store, comment = ready_store(tmp_path)
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99'),
                                   dm_reply=lambda *_: asyncio.sleep(0, result='收到，我来看看。'),
                                   comment_reply=lambda *_: asyncio.sleep(0, result='评论答复'),
                                   sleep=lambda _: asyncio.sleep(0))
        assert await listener.poll_once() == 0
        fake.state.dm_messages.extend([dm(12, uid=77), dm(13, body='新的问题')])
        fake.state.reply_notifications.extend([notice(11, root=999), notice(12, body='新回复')])
        assert await listener.poll_once() == 2
        assert await listener.poll_once() == 0
    dm_writes = [form for path, form in calls if path.endswith('/send_msg')]
    comment_writes = [form for path, form in calls if path == 'x/v2/reply/add']
    assert len(dm_writes) == len(comment_writes) == 1
    assert dm_writes[0]['msg[receiver_id]'] == '42'
    assert json.loads(dm_writes[0]['msg[content]'])['content'] == '收到，我来看看。'
    assert comment_writes[0]['root'] == '55' and comment_writes[0]['parent'] == '112'
    assert store.reply_count(comment) == 1


@pytest.mark.asyncio
async def test_listener_dry_run_never_sends_and_does_not_arm_live_cursor(tmp_path):
    fake, calls = make_fake_bili()
    fake.state.dm_messages.append(dm(10))
    store, _ = ready_store(tmp_path)
    settings = Settings(owner_uid=42, dry_run=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99'),
                                   sleep=lambda _: asyncio.sleep(0))
        await listener.poll_once()
        fake.state.dm_messages.append(dm(11))
        await listener.poll_once()
    assert not any(path.endswith('/send_msg') or path == 'x/v2/reply/add' for path, _ in calls)
    assert store.get_setting('listener_dm_cursor_99_42') is None


@pytest.mark.asyncio
async def test_listener_restart_keeps_cursors_and_does_not_repeat_messages(tmp_path):
    fake, calls = make_fake_bili()
    fake.state.dm_messages.append(dm(10))
    store, _ = ready_store(tmp_path)
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        bili = BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99')
        make_listener = lambda: MessageListener(
            settings, store, bili, dm_reply=lambda *_: asyncio.sleep(0, result='收到。'),
            sleep=lambda _: asyncio.sleep(0))
        await make_listener().poll_once()
        fake.state.dm_messages.append(dm(11))
        assert await make_listener().poll_once() == 1
        assert await make_listener().poll_once() == 0
    assert store.get_setting('listener_dm_cursor_99_42') == '11'
    assert len([path for path, _ in calls if path.endswith('/send_msg')]) == 1


@pytest.mark.asyncio
async def test_listener_509_stops_before_second_write_and_never_retries_claim(tmp_path):
    fake, calls = make_fake_bili(dm_fail_code=-509)
    fake.state.dm_messages.append(dm(10))
    store, _ = ready_store(tmp_path)
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99'),
                                   dm_reply=lambda *_: asyncio.sleep(0, result='答复'), sleep=lambda _: asyncio.sleep(0))
        assert await listener.poll_once() == 0
        fake.state.dm_messages.extend([dm(11), dm(12)])
        with pytest.raises(BiliError) as exc:
            await listener.poll_once()
        assert exc.value.stop_run
    assert len([path for path, _ in calls if path.endswith('/send_msg')]) == 1
    assert store.dm_reply_count() == 1


@pytest.mark.asyncio
async def test_listener_waits_between_writes_and_caps_each_poll(tmp_path):
    fake, calls = make_fake_bili()
    fake.state.dm_messages.append(dm(10))
    store, _ = ready_store(tmp_path)
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake',
                        listen_max_per_cycle=2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99'),
                                   dm_reply=lambda *_: asyncio.sleep(0, result='收到。'), sleep=record_wait)
        await listener.poll_once()
        fake.state.dm_messages.extend([dm(11), dm(12), dm(13)])
        assert await listener.poll_once() == 2
        assert await listener.poll_once() == 1
    assert len([path for path, _ in calls if path.endswith('/send_msg')]) == 3
    assert len(waits) == 2
    assert all(5 <= seconds <= 20 for seconds in waits)


@pytest.mark.asyncio
async def test_listener_stops_when_dm_page_cannot_cover_all_new_messages(tmp_path):
    fake, calls = make_fake_bili()
    fake.state.dm_messages.append(dm(10))
    store, _ = ready_store(tmp_path)
    settings = Settings(owner_uid=42, dry_run=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf; DedeUserID=99'))
        await listener.poll_once()
        fake.state.dm_messages.extend(dm(seq) for seq in range(11, 70))
        with pytest.raises(RuntimeError, match='单页容量'):
            await listener.poll_once()
    assert not any(path.endswith('/send_msg') for path, _ in calls)


@pytest.mark.asyncio
async def test_private_chat_model_uses_persona_and_only_prior_conversation(tmp_path):
    requests = []

    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '我会先确认具体步骤。'}}]})

    store, _ = ready_store(tmp_path)
    settings = Settings(owner_uid=42, persona='简短、友好', model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake', dry_run=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as model:
        listener = MessageListener(settings, store, None, model_http=model)
        listener.account_uid = 99
        history = [dm(10, body='第一问'), dm(11, uid=99, body='先看看'),
                   dm(12, body='第二问'), dm(13, body='未来消息')]
        result = await listener._model_dm('第二问', history[:3])
    assert result == '我会先确认具体步骤。'
    assert '简短、友好' in requests[0]['messages'][0]['content']
    assert '不执行' in requests[0]['messages'][0]['content']
    assert '未来消息' not in json.dumps(requests[0], ensure_ascii=False)
    assert requests[0]['messages'][-1]['content'] == '第二问'
    assert requests[0]['messages'][-2]['role'] == 'assistant'


@pytest.mark.asyncio
async def test_listener_command_requires_model_before_login(tmp_path):
    fake, calls = make_fake_bili()
    store, _ = ready_store(tmp_path)
    messages = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        code = await cli.run_command('listen', Settings(owner_uid=42, dry_run=False), store,
                                     bili_http=http, output=messages.append)
    assert code == 1
    assert not calls
    assert any('MODEL_' in message for message in messages)


@pytest.mark.asyncio
async def test_listen_command_scans_then_reads_messages_without_video_browsing(tmp_path):
    fake, calls = make_fake_bili()
    store, _ = ready_store(tmp_path)
    settings = Settings(owner_uid=42, dry_run=True)
    lines = []

    async def stop_after_first_poll(_):
        raise asyncio.CancelledError()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        with pytest.raises(asyncio.CancelledError):
            await cli.run_command('listen', settings, store, bili_http=http,
                                  sleep=stop_after_first_poll, output=lines.append)
    paths = [path for path, _ in calls]
    assert paths.index('x/passport-login/web/qrcode/poll') < paths.index('svr_sync/v1/svr_sync/fetch_session_msgs')
    assert 'x/msgfeed/reply' in paths
    assert 'x/msgfeed/at' in paths
    assert not any(path.endswith('search/type') or path == 'x/web-interface/view' for path in paths)
    assert any('按 Ctrl+C' in line for line in lines)


def test_listener_config_is_bounded_and_chinese_documented(tmp_path):
    path = tmp_path / '.env'
    path.write_text('BILI_LISTEN_INTERVAL_SECONDS=60\nBILI_LISTEN_MAX_PER_CYCLE=2\n'
                    'BILI_LISTEN_MAX_PER_SESSION=10\nBILI_LISTENER_MAX_AGE_HOURS=12\n', encoding='utf-8')
    settings = Settings.from_env(path)
    assert (settings.listen_interval_seconds, settings.listen_max_per_cycle,
            settings.listen_max_per_session, settings.listener_max_age_hours) == (60, 2, 10, 12)
    path.write_text('BILI_LISTEN_INTERVAL_SECONDS=5\n', encoding='utf-8')
    with pytest.raises(ValueError, match='至少为 60'):
        Settings.from_env(path)
    path.write_text('BILI_LISTENER_MAX_AGE_HOURS=200\n', encoding='utf-8')
    with pytest.raises(ValueError, match='1～168'):
        Settings.from_env(path)


@pytest.mark.asyncio
async def test_owner_shared_video_card_is_read_with_subtitles_then_answered_once(tmp_path):
    fake, calls = make_fake_bili()
    store, _ = ready_store(tmp_path)
    fake.state.dm_messages.append(dm(10))
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    requests = []

    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '这条讲先最小复现，再定位问题。'}}]})

    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(answer)) as model):
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   model_http=model, sleep=lambda _: asyncio.sleep(0))
        assert await listener.poll_once() == 0
        fake.state.dm_messages.append({'msg_seqno': 11, 'msg_key': 110, 'sender_uid': 42,
                                       'msg_type': 7, 'msg_status': 0,
                                       'content': json.dumps({'source': 5, 'bvid': 'BV1test', 'title': 'Python 调试'})})
        assert await listener.poll_once() == 1
        assert await listener.poll_once() == 0
    paths = [path for path, _ in calls]
    assert 'x/web-interface/view' in paths and 'x/player/wbi/v2' in paths
    assert '先最小复现' in json.dumps(requests[0], ensure_ascii=False)
    assert len([path for path in paths if path.endswith('/send_msg')]) == 1
    assert not any(path in ('x/web-interface/archive/like', 'x/v3/fav/resource/deal', 'x/v2/reply/add') for path in paths)


@pytest.mark.asyncio
async def test_owner_video_link_without_subtitles_gets_honest_answer(tmp_path):
    fake, calls = make_fake_bili(subtitles=False)
    store, _ = ready_store(tmp_path)
    fake.state.dm_messages.append(dm(10))
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    requests = []

    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '我只拿到标题和简介，没拿到字幕，无法确认视频讲解。'}}]})

    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(answer)) as model):
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   model_http=model, sleep=lambda _: asyncio.sleep(0))
        await listener.poll_once()
        fake.state.dm_messages.append(dm(11, body='看看 https://www.bilibili.com/video/BV1test 这条怎么样？'))
        assert await listener.poll_once() == 1
    assert not requests
    writes = [form for path, form in calls if path.endswith('/send_msg')]
    assert len(writes) == 1
    assert '没拿到字幕' in json.loads(writes[0]['msg[content]'])['content']


@pytest.mark.asyncio
async def test_hidden_mention_reply_falls_back_to_private_message(tmp_path):
    fake, calls = make_fake_bili()
    store, _ = ready_store(tmp_path)
    fake.state.dm_messages.append(dm(10))
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json={'choices': [{'message': {'content': '先看字幕里的最小复现。'}}]}))) as model):
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   model_http=model, sleep=lambda _: asyncio.sleep(0))
        await listener.poll_once()
        fake.state.at_notifications.append(at_notice(11))
        assert await listener.poll_once() == 1
    writes = [form for path, form in calls if path.endswith('/send_msg')]
    assert len(writes) == 1
    text = json.loads(writes[0]['msg[content]'])['content']
    assert '公开回复被 B 站隐藏' in text
    assert '先看字幕里的最小复现' in text


@pytest.mark.asyncio
async def test_visible_mention_reply_does_not_send_fallback_dm(tmp_path):
    fake, calls = make_fake_bili(public_reply_visible=True)
    store, _ = ready_store(tmp_path)
    fake.state.dm_messages.append(dm(10))
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json={'choices': [{'message': {'content': '先看字幕里的最小复现。'}}]}))) as model):
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   model_http=model, sleep=lambda _: asyncio.sleep(0))
        await listener.poll_once()
        fake.state.at_notifications.append(at_notice(11))
        assert await listener.poll_once() == 1
    assert not any(path.endswith('/send_msg') for path, _ in calls)


@pytest.mark.asyncio
async def test_owner_at_in_at_feed_replies_to_top_level_comment(tmp_path):
    fake, calls = make_fake_bili()
    store, _ = ready_store(tmp_path)
    fake.state.dm_messages.append(dm(10))
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json={'choices': [{'message': {'content': '先看字幕里的最小复现。'}}]}))) as model):
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   model_http=model, sleep=lambda _: asyncio.sleep(0))
        assert await listener.poll_once() == 0
        fake.state.at_notifications.append(at_notice(11))
        assert await listener.poll_once() == 1
        assert await listener.poll_once() == 0
    writes = [form for path, form in calls if path == 'x/v2/reply/add']
    assert len(writes) == 1
    assert writes[0]['oid'] == '1'
    assert writes[0]['root'] == writes[0]['parent'] == '211'
    assert store.get_setting('listener_at_cursor_99') == '11'


@pytest.mark.asyncio
async def test_same_mention_in_both_feeds_replies_once(tmp_path):
    fake, calls = make_fake_bili()
    store, _ = ready_store(tmp_path)
    fake.state.dm_messages.append(dm(10))
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    duplicated = {'id': 12, 'reply_time': 12, 'user': {'mid': 42},
                  'item': {'business_id': 1, 'subject_id': 1, 'root_id': 0,
                           'source_id': 211, 'source_content': '@小号 总结一下',
                           'at_details': [{'mid': 99}]}}
    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json={'choices': [{'message': {'content': '先看字幕里的最小复现。'}}]}))) as model):
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   model_http=model, sleep=lambda _: asyncio.sleep(0))
        await listener.poll_once()
        fake.state.at_notifications.append(at_notice(11))
        fake.state.reply_notifications.append(duplicated)
        assert await listener.poll_once() == 1
    writes = [form for path, form in calls if path == 'x/v2/reply/add']
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_owner_at_on_other_video_reads_subtitles_and_publicly_replies(tmp_path):
    fake, calls = make_fake_bili()
    store, _ = ready_store(tmp_path)
    fake.state.dm_messages.append(dm(10))
    fake.state.reply_notifications.append(at_notice(10))
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    requests = []

    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '我看字幕里讲的是先复现再定位。'}}]})

    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(answer)) as model):
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   model_http=model, sleep=lambda _: asyncio.sleep(0))
        assert await listener.poll_once() == 0
        fake.state.reply_notifications.extend([at_notice(11, uid=77), at_notice(12)])
        assert await listener.poll_once() == 1
        assert await listener.poll_once() == 0
    writes = [form for path, form in calls if path == 'x/v2/reply/add']
    assert len(writes) == 1 and writes[0]['oid'] == '1'
    assert writes[0]['root'] == writes[0]['parent'] == '212'
    assert '先最小复现' in json.dumps(requests[0], ensure_ascii=False)
    assert store.dashboard_data()['totals']['replies_ok'] == 1


@pytest.mark.asyncio
async def test_dry_run_shared_video_does_not_call_model_or_send(tmp_path):
    fake, calls = make_fake_bili()
    store, _ = ready_store(tmp_path)
    fake.state.dm_messages.append(dm(10))
    settings = Settings(owner_uid=42, dry_run=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   sleep=lambda _: asyncio.sleep(0))
        await listener.poll_once()
        fake.state.dm_messages.append(dm(11, body='BV1test 这个怎么样？'))
        assert await listener.poll_once() == 0
    assert not any(path == 'x/web-interface/view' or path.endswith('/send_msg') for path, _ in calls)


@pytest.mark.asyncio
async def test_video_card_with_aid_is_supported_and_risk_error_stops(tmp_path):
    fake, calls = make_fake_bili()
    store, _ = ready_store(tmp_path)
    fake.state.dm_messages.append(dm(10))
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http:
        bili = BiliClient(http, 'SESSDATA=x; bili_jct=csrf')
        listener = MessageListener(settings, store, bili,
                                   dm_reply=lambda *_: asyncio.sleep(0, result='看过字幕了。'),
                                   sleep=lambda _: asyncio.sleep(0))
        await listener.poll_once()
        fake.state.dm_messages.append({'msg_seqno': 11, 'msg_key': 110, 'sender_uid': 42,
                                       'msg_type': 7, 'msg_status': 0,
                                       'content': json.dumps({'source': 5, 'id': 1, 'title': 'Python 调试'})})
        assert await listener.poll_once() == 1
        async def risk(_aid):
            raise BiliError(412, '风控')
        bili.detail_by_aid = risk
        fake.state.reply_notifications.append(at_notice(11))
        with pytest.raises(BiliError) as exc:
            await listener.poll_once()
        assert exc.value.stop_run
    assert any(path == 'x/web-interface/view' and form.get('aid') == '1' for path, form in calls)
    assert len([path for path, _ in calls if path == 'x/v2/reply/add']) == 0


@pytest.mark.asyncio
async def test_followup_after_shared_video_uses_subtitles_across_listener_restart(tmp_path):
    fake, calls = make_fake_bili()
    fake.state.dm_messages.append(dm(10))
    store, _ = ready_store(tmp_path)
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    requests = []

    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '先最小复现，再定位问题。'}}]})

    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(answer)) as model):
        bili = BiliClient(http, 'SESSDATA=x; bili_jct=csrf')
        make_listener = lambda: MessageListener(settings, store, bili, model_http=model,
                                                sleep=lambda _: asyncio.sleep(0))
        first = make_listener()
        assert await first.poll_once() == 0
        fake.state.dm_messages.append({'msg_seqno': 11, 'msg_key': 110, 'sender_uid': 42,
                                       'msg_type': 7, 'msg_status': 0,
                                       'content': json.dumps({'source': 5, 'bvid': 'BV1test', 'title': 'Python 调试'})})
        assert await first.poll_once() == 1
        fake.state.dm_messages.append(dm(12, body='刚才那条第二点呢？'))
        assert await make_listener().poll_once() == 1

    assert len(requests) == 2
    followup = requests[1]['messages'][-1]['content']
    assert '刚才那条第二点呢' in followup and '先最小复现，再定位问题' in followup
    assert len([path for path, _ in calls if path == 'x/web-interface/view']) == 1
    assert len([path for path, _ in calls if path.endswith('/send_msg')]) == 2


@pytest.mark.asyncio
async def test_unrelated_chat_does_not_pull_in_old_video_context(tmp_path):
    fake, _ = make_fake_bili()
    fake.state.dm_messages.append(dm(10))
    store, _ = ready_store(tmp_path)
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    requests = []

    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '今天可以先休息。'}}]})

    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(answer)) as model):
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   model_http=model, sleep=lambda _: asyncio.sleep(0))
        await listener.poll_once()
        fake.state.dm_messages.append(dm(11, body='BV1test 这个讲什么？'))
        assert await listener.poll_once() == 1
        fake.state.dm_messages.append(dm(12, body='今天吃什么？'))
        assert await listener.poll_once() == 1
        fake.state.dm_messages.append(dm(13, body='推荐个视频吧'))
        assert await listener.poll_once() == 1
    assert '先最小复现' not in requests[-2]['messages'][-1]['content']
    assert '先最小复现' not in requests[-1]['messages'][-1]['content']


def test_video_memory_is_scoped_and_expires(tmp_path):
    store, _ = ready_store(tmp_path)
    store.save_video_memory(99, 42, 'BV1test', '标题：Python 调试\n字幕：先最小复现。', True)
    assert '先最小复现' in store.get_video_memory(99, 42, max_age_hours=2)['context']
    assert store.get_video_memory(100, 42, max_age_hours=2) is None
    assert store.get_video_memory(99, 77, max_age_hours=2) is None
    with store.connect() as db:
        db.execute("UPDATE video_memory SET updated_at='2020-01-01T00:00:00+00:00'")
    assert store.get_video_memory(99, 42, max_age_hours=2) is None


@pytest.mark.asyncio
async def test_no_subtitles_followup_stays_honest_and_does_not_call_model(tmp_path):
    fake, calls = make_fake_bili(subtitles=False)
    fake.state.dm_messages.append(dm(10))
    store, _ = ready_store(tmp_path)
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    requests = []

    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '不应调用模型。'}}]})

    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(answer)) as model):
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   model_http=model, sleep=lambda _: asyncio.sleep(0))
        await listener.poll_once()
        fake.state.dm_messages.append(dm(11, body='BV1test 讲了什么？'))
        assert await listener.poll_once() == 1
        fake.state.dm_messages.append(dm(12, body='刚才那条第二点呢？'))
        assert await listener.poll_once() == 1
    assert not requests
    answers = [json.loads(form['msg[content]'])['content'] for path, form in calls if path.endswith('/send_msg')]
    assert len(answers) == 2 and all('没拿到字幕' in text for text in answers)


@pytest.mark.asyncio
async def test_owner_at_video_can_be_followed_up_in_dm(tmp_path):
    fake, calls = make_fake_bili()
    fake.state.dm_messages.append(dm(10))
    store, _ = ready_store(tmp_path)
    settings = Settings(owner_uid=42, dry_run=False, model_name='fake',
                        model_base_url='https://model.test', model_api_key='fake')
    requests = []

    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '先最小复现。'}}]})

    async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url='https://fake.test') as http,
                httpx.AsyncClient(transport=httpx.MockTransport(answer)) as model):
        listener = MessageListener(settings, store, BiliClient(http, 'SESSDATA=x; bili_jct=csrf'),
                                   model_http=model, sleep=lambda _: asyncio.sleep(0))
        await listener.poll_once()
        fake.state.reply_notifications.append(at_notice(11))
        assert await listener.poll_once() == 1
        fake.state.dm_messages.append(dm(11, body='刚才那条具体怎么操作？'))
        assert await listener.poll_once() == 1
    assert len(requests) == 2
    assert '先最小复现' in requests[-1]['messages'][-1]['content']
    assert len([path for path, _ in calls if path == 'x/web-interface/view']) == 1


def test_video_memory_config_is_bounded_and_chinese_documented(tmp_path):
    path = tmp_path / '.env'
    path.write_text('BILI_VIDEO_MEMORY_HOURS=3\n', encoding='utf-8')
    assert Settings.from_env(path).video_memory_hours == 3
    path.write_text('BILI_VIDEO_MEMORY_HOURS=25\n', encoding='utf-8')
    with pytest.raises(ValueError, match='1～24'):
        Settings.from_env(path)
    example = (Path(__file__).resolve().parents[1] / '.env.example').read_text(encoding='utf-8')
    assert 'BILI_VIDEO_MEMORY_HOURS=2' in example and '最近一条分享视频' in example

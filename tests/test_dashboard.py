"""The dashboard reads only local fake run data; no Bilibili account is used."""

import http.client
import threading
from http.server import ThreadingHTTPServer

from study_scout.dashboard import make_handler, render_dashboard
from study_scout.judge import Judgment
from study_scout.storage import Store


def test_dashboard_shows_run_video_decision_and_interaction_status(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    run_id = store.start_run()
    candidate = store.add_candidate(run_id, {
        'bvid': 'BV1test', 'aid': 1, 'cid': 2, 'title': '<script>alert(1)</script>',
        'author': '老师',
    })
    store.record_judgment(candidate, Judgment(worth_learning=True, score=86, reason='讲了具体步骤',
                                               takeaways=['先复现'], risk='无'), 'fake')
    store.record_action(candidate, 'like', 'ok')
    store.record_action(candidate, 'favorite', 'failed', error='-111')
    store.record_action(candidate, 'private_message', 'failed', error='-111')
    store.save_comment(candidate, 321, '这个例子有用', 42, False, 'ok')
    store.update_run_counts(run_id, 1, 1)
    store.finish_run(run_id, 'failed', '-111')

    snapshot = store.dashboard_data()
    html = render_dashboard(snapshot)

    assert snapshot['totals']['videos'] == 1
    assert snapshot['selected_run']['id'] == run_id
    assert snapshot['videos'][0]['actions']['private_message'] == 'failed'
    assert 'BV1test' in html and '讲了具体步骤' in html
    assert '私信失败' in html and '接口已接受' in html
    assert '&lt;script&gt;' in html and '<script>alert(1)</script>' not in html
    assert 'MODEL_API_KEY' not in html


def test_dashboard_records_why_inspected_video_was_skipped(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    run_id = store.start_run()
    candidate = store.add_candidate(run_id, {'bvid': 'BVmissing', 'aid': 3, 'cid': 4,
                                              'title': '无字幕视频'})
    store.record_skip(candidate, 'no_subtitles')
    store.update_run_counts(run_id, 1, 0)
    store.finish_run(run_id, 'done')

    snapshot = store.dashboard_data()
    html = render_dashboard(snapshot)
    assert snapshot['videos'][0]['skip_reason'] == 'no_subtitles'
    assert '无字幕视频' in html and '无字幕，跳过' in html


def test_existing_database_gets_skip_reason_column_without_losing_rows(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    run_id = store.start_run()
    store.add_candidate(run_id, {'bvid': 'BVold', 'title': '旧视频'})
    with store.connect() as db:
        db.execute('ALTER TABLE candidates DROP COLUMN skip_reason')

    store.init()
    assert store.dashboard_data()['videos'][0]['bvid'] == 'BVold'


def test_dashboard_http_is_local_read_only_and_does_not_expose_secrets(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.init()
    with ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store)) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('GET', '/')
            response = connection.getresponse()
            body = response.read().decode('utf-8')
            assert response.status == 200
            assert '只读展示' in body
            assert response.getheader('Cache-Control') == 'no-store'
            assert 'MODEL_API_KEY' not in body
            connection.request('POST', '/')
            assert connection.getresponse().status == 501
            connection.close()

            hostile = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            hostile.request('GET', '/', headers={'Host': 'other.example'})
            assert hostile.getresponse().status == 403
            hostile.close()
        finally:
            server.shutdown()
            worker.join(timeout=3)

"""One foreground process schedules video rounds and message checks."""

import asyncio
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


class RunController:
    """Thread-safe, bounded bridge from the local web button to the async loop."""

    def __init__(self, settings):
        self.token = secrets.token_urlsafe(32)
        self.settings = settings
        self._lock = threading.Lock()
        self._loop = None
        self._wake = None
        self._state = 'idle'
        self._pending = False
        self._stopping = False
        self._last_run_id = None
        self._last_status = None
        self._error = None
        self._next_run_at = None
        self._listener_paused = False

    def bind_loop(self):
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()

    def request_run(self):
        with self._lock:
            if self._stopping or self._state != 'idle' or self._pending:
                return False
            self._pending = True
        if self._loop and self._wake:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                self.stop()
                return False
        return True

    def take_pending(self):
        with self._lock:
            pending = self._pending
            self._pending = False
            return pending

    def start_activity(self, name):
        with self._lock:
            self._state = name
            self._pending = False

    def finish_activity(self):
        with self._lock:
            if not self._stopping:
                self._state = 'idle'

    def record_run(self, run_id, status, error=None):
        with self._lock:
            self._last_run_id = run_id
            self._last_status = status
            self._error = error

    def next_run_in(self, seconds):
        value = datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))
        with self._lock:
            self._next_run_at = value.isoformat()

    def pause_listener(self):
        with self._lock:
            self._listener_paused = True

    def stop(self):
        with self._lock:
            self._stopping = True
            self._state = 'stopped'
            self._pending = False
            self._next_run_at = None
        if self._loop and self._wake:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass  # The web server and event loop are already closing.

    @property
    def stopping(self):
        with self._lock:
            return self._stopping

    def snapshot(self):
        with self._lock:
            return {'state': self._state, 'pending': self._pending,
                    'dry_run': self.settings.dry_run,
                    'interval_minutes': self.settings.run_interval_minutes,
                    'next_run_at': self._next_run_at,
                    'last_run_id': self._last_run_id,
                    'last_status': self._last_status,
                    'error': self._error,
                    'listener_paused': self._listener_paused}

    async def wait(self, timeout):
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0, timeout))
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()


class StudyService:
    def __init__(self, settings, store, runner, listener, controller, *,
                 clock=time.monotonic, wait=None, output=print):
        self.settings, self.store = settings, store
        self.runner, self.listener = runner, listener
        self.controller = controller
        self.clock, self.wait, self.output = clock, wait or controller.wait, output

    async def _run_video(self):
        self.controller.start_activity('running')
        try:
            run_id = await self.runner.run_once(check_replies=False)
            run = self.store.recent_runs()[0]
            self.controller.record_run(run_id, run['status'], run['error_code'])
            self.output(f"本轮 {run_id}：{run['status']}，候选 {run['candidates_count']}，入选 {run['selected_count']}，错误 {run['error_code'] or '无'}")
            if run['status'] != 'done':
                self.controller.stop()
                self.output('本轮未成功，已停止自动运行；检查原因后重新启动。')
        except Exception:
            self.controller.stop()
            raise
        finally:
            self.controller.finish_activity()

    async def _poll_messages(self):
        if self.listener.sent_total >= self.settings.listen_max_per_session:
            self.controller.pause_listener()
            return
        self.controller.start_activity('listening')
        try:
            await self.listener.poll_once()
        except Exception:
            self.controller.stop()
            raise
        finally:
            self.controller.finish_activity()
        if self.listener.sent_total >= self.settings.listen_max_per_session:
            self.controller.pause_listener()
            self.output('本次登录的消息自动回复已达到上限；视频轮次继续运行。')

    async def run_forever(self):
        self.controller.bind_loop()
        listen_enabled = self.listener is not None and self.settings.owner_uid > 0
        if listen_enabled:
            await self._poll_messages()  # Establish the baseline before the first video round.
        if self.controller.stopping:
            return
        await self._run_video()
        if self.controller.stopping:
            return
        interval = self.settings.run_interval_minutes * 60
        next_video = self.clock() + interval
        next_listener = self.clock() + self.settings.listen_interval_seconds if listen_enabled else None
        self.controller.next_run_in(interval)
        log.info('服务进入常驻：视频每 %s 分钟一轮，消息每 %s 秒检查',
                 self.settings.run_interval_minutes, self.settings.listen_interval_seconds)
        while not self.controller.stopping:
            now = self.clock()
            if self.controller.take_pending() or now >= next_video:
                await self._run_video()
                if self.controller.stopping:
                    break
                next_video = self.clock() + interval
                self.controller.next_run_in(interval)
                continue
            if (listen_enabled and not self.controller.snapshot()['listener_paused']
                    and now >= next_listener):
                await self._poll_messages()
                next_listener = self.clock() + self.settings.listen_interval_seconds
                continue
            due = [next_video]
            if listen_enabled and not self.controller.snapshot()['listener_paused']:
                due.append(next_listener)
            await self.wait(max(0, min(due) - now))

"""Non-blocking, process-wide lock for commands that can write to Bilibili."""

import os
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    """Another Study Scout command owns the session lock."""


@contextmanager
def single_instance(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock_file:
        if os.name == 'nt':
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b'\0')
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise AlreadyRunning('已有一个 Study Scout 实例在运行') from exc
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise AlreadyRunning('已有一个 Study Scout 实例在运行') from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

"""跨进程的那把锁：同一时刻只有一个进程拿同一把领星 key 取数（网关 QPS=1）。

插件形态下 SFW 每个对话各拉一个进程，进程里的 threading.Lock 管不到别的对话：
两个对话同时问，就是两个进程并发取数、同时写运行记录（2026-09-23 Codex 复审 P2）。
flock 跟着打开的文件走，进程死了锁自己就放了。找否定词和操盘手看一遍共用这一把。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from pathlib import Path

#: 跨进程那把锁的文件名，放在运行记录旁边：那一层两种形态下都是本服务自己写的。
RUN_LOCK_NAME = ".running.lock"


@contextlib.contextmanager
def one_process_at_a_time(lock_path: Path, wait_seconds: float) -> Iterator[bool]:
    """拿到了 yield True，等满 wait_seconds 还拿不到 yield False。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.2)
            else:
                yield True
                return
    finally:
        os.close(fd)

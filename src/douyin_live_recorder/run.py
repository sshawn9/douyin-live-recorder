from __future__ import annotations

import asyncio
import fcntl
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from .data import DouyinLiveData
from .display import display
from .downloader import download_segments, retry_segments
from .output import merge_segments
from .producer import produce_playlists


async def start(data: DouyinLiveData) -> None:
    data.recordings_dir = data.recordings_dir.expanduser().resolve()
    data.user_dir = data.recordings_dir / data.user_id
    data.runtime_dir = data.user_dir / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    data.segment_dir = data.runtime_dir / "segments"

    with recorder_lock(data), stop_signals(data):
        data.segment_dir.mkdir(parents=True)
        data.status = "Monitoring live stream"
        data.log(
            f"session started: pid={os.getpid()} user={data.user_id} "
            f"runtime_dir={str(data.runtime_dir)!r}"
        )
        display_task = asyncio.create_task(
            display(data),
            name="display",
        )
        producer = asyncio.create_task(
            produce_playlists(data),
            name="playlist-producer",
        )
        downloader = asyncio.create_task(
            download_segments(data),
            name="segment-downloader",
        )
        retry = asyncio.create_task(
            retry_segments(data),
            name="segment-retry",
        )
        try:
            await producer
            await data.segment_jobs.join()
            await data.retry_jobs.join()
        finally:
            data.status = "Stopping"
            data.stop_event.set()
            data.segment_jobs.shutdown()
            data.retry_jobs.shutdown()
            await asyncio.gather(producer, downloader, retry, return_exceptions=True)
            try:
                await merge_segments(data)
            finally:
                display_task.cancel()
                await asyncio.gather(display_task, return_exceptions=True)


@contextmanager
def recorder_lock(data: DouyinLiveData) -> Iterator[None]:
    lock_path = data.recordings_dir / data.user_id / ".recorder.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_handle.seek(0)
            pid = lock_handle.read().strip() or "unknown"
            raise RuntimeError(
                f"A recorder is already running for this user (PID {pid})"
            ) from error

        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(str(os.getpid()))
        lock_handle.flush()
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def stop_signals(data: DouyinLiveData) -> Iterator[None]:
    loop = asyncio.get_running_loop()
    installed = []
    try:
        for watched_signal in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(watched_signal, data.stop_event.set)
            except NotImplementedError:
                continue
            installed.append(watched_signal)
        yield
    finally:
        for watched_signal in installed:
            loop.remove_signal_handler(watched_signal)

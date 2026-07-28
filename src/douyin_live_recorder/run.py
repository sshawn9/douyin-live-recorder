from __future__ import annotations

import asyncio
import fcntl
import os
import signal

from .data import DouyinLiveData
from .monitor import monitor
from .recording import manage_recording


async def run(data: DouyinLiveData) -> None:
    data.output_dir = data.output_dir.expanduser().resolve()

    lock_path = data.output_dir / data.user_id / ".recorder.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_handle.seek(0)
            pid = lock_handle.read().strip() or "unknown"
            raise RuntimeError(f"A monitor is already running for this user (PID {pid})") from exc

        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(str(os.getpid()))
        lock_handle.flush()

        loop = asyncio.get_running_loop()
        installed_signals: list[signal.Signals] = []
        try:
            for watched_signal in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(
                        watched_signal,
                        data.stop_event.set,
                    )
                except NotImplementedError:
                    continue
                installed_signals.append(watched_signal)

            async with asyncio.TaskGroup() as tasks:
                monitor_task = tasks.create_task(monitor(data))
                tasks.create_task(manage_recording(data))
                await data.stop_event.wait()
                monitor_task.cancel()
        finally:
            for watched_signal in installed_signals:
                loop.remove_signal_handler(watched_signal)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

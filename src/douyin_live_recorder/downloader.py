from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx

from .data import USER_AGENT, DouyinLiveData, Segment


async def download_segments(data: DouyinLiveData) -> None:
    assert data.segment_dir is not None

    client = httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://live.douyin.com/",
        },
        timeout=httpx.Timeout(5.0),
        follow_redirects=True,
    )
    async with client:
        workers = [
            asyncio.create_task(
                download_worker(data, client),
                name=f"hls-download-{number}",
            )
            for number in range(1, 4)
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


async def retry_segments(data: DouyinLiveData) -> None:
    assert data.segment_dir is not None

    client = httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://live.douyin.com/",
        },
        timeout=httpx.Timeout(5.0),
        follow_redirects=True,
    )
    async with client:
        semaphore = asyncio.Semaphore(2)
        async with asyncio.TaskGroup() as tasks:
            while True:
                try:
                    segment = await data.retry_jobs.get()
                except asyncio.QueueShutDown:
                    return

                tasks.create_task(
                    retry_segment(data, client, semaphore, segment),
                    name=f"hls-retry-{segment.playlist_index}-{segment.sequence}",
                )


async def download_worker(
    data: DouyinLiveData,
    client: httpx.AsyncClient,
) -> None:
    while True:
        try:
            segment = await data.segment_jobs.get()
        except asyncio.QueueShutDown:
            return

        try:
            if segment.bytes_written:
                continue

            segment.attempts += 1
            error = await download_segment(segment, client)
            if error is None:
                continue

            data.log(
                f"download segment failed: playlist={segment.playlist_index} "
                f"sequence={segment.sequence} error={error}; queued for retry"
            )
            segment.failed_at = time.monotonic()
            try:
                data.retry_jobs.put_nowait(segment)
            except asyncio.QueueShutDown:
                data.log(
                    f"segment abandoned: playlist={segment.playlist_index} "
                    f"sequence={segment.sequence} reason=retry_queue_closed "
                    f"error={error}"
                )
        finally:
            data.segment_jobs.task_done()


async def retry_segment(
    data: DouyinLiveData,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    segment: Segment,
) -> None:
    assert segment.failed_at is not None
    try:
        delay = 0.0
        while time.monotonic() - segment.failed_at < 30.0:
            async with semaphore:
                if segment.bytes_written:
                    return
                segment.attempts += 1
                error = await download_segment(segment, client)

            if error is None:
                return
            delay += 0.1
            delay = min(1.0, delay)
            data.log(
                f"retry segment failed: playlist={segment.playlist_index} "
                f"sequence={segment.sequence} error={error}; queued for retry after {delay}s"
            )
            await asyncio.sleep(delay)

        data.failed.append(segment)
        data.log(
            f"segment download failed: playlist={segment.playlist_index} "
            f"sequence={segment.sequence} attempts={segment.attempts} "
            "reason=retry_window_expired retry_window=30s"
        )
    finally:
        data.retry_jobs.task_done()


async def download_segment(
    segment: Segment,
    client: httpx.AsyncClient,
) -> str | None:
    temporary = segment.path.with_suffix(".part")
    temporary.unlink(missing_ok=True)
    bytes_written = 0

    try:
        async with client.stream("GET", segment.uri) as response:
            response.raise_for_status()
            expected = response.headers.get("content-length")
            with temporary.open("wb") as handle:
                async for chunk in response.aiter_raw():
                    handle.write(chunk)
                    bytes_written += len(chunk)

        if bytes_written == 0:
            raise ValueError("empty segment data")
        if expected is not None and bytes_written != int(expected):
            raise ValueError(f"incomplete response: expected={expected} actual={bytes_written}")
        if not is_mpegts(temporary):
            raise ValueError("invalid MPEG-TS data")

        temporary.replace(segment.path)
        segment.bytes_written = bytes_written
        return None
    except httpx.HTTPStatusError as error:
        result = f"HTTP {error.response.status_code}"
    except httpx.HTTPError as error:
        result = type(error).__name__
    except ValueError as error:
        result = str(error)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    temporary.unlink(missing_ok=True)
    return result


def is_mpegts(path: Path) -> bool:
    size = path.stat().st_size
    if size < 188 * 5 or size % 188 != 0:
        return False
    with path.open("rb") as handle:
        sample = handle.read(188 * 8)
    return all(sample[offset] == 0x47 for offset in range(0, len(sample), 188))

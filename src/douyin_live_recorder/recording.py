from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime

from .data import USER_AGENT, DouyinLiveData
from .runtime import wait


def log(message: str) -> None:
    now = datetime.now(UTC).astimezone()
    print(f"{now:%H:%M:%S} Recording: {message}", flush=True)


async def start_recording(data: DouyinLiveData) -> None:
    if data.ffmpeg_process is not None or data.stream_url is None:
        return

    directory = data.recording_directory
    if directory is None:
        directory = (
            data.output_dir
            / data.user_id
            / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        )
        directory.mkdir(parents=True)
        data.recording_directory = directory

    indexes = []
    for path in directory.glob("part-*.ts"):
        index = path.stem.removeprefix("part-")
        if index.isdigit():
            indexes.append(int(index))
    output = directory / f"part-{max(indexes, default=0) + 1:04d}.ts"

    data.ffmpeg_process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-stdin",
        "-loglevel",
        "warning",
        "-live_start_index",
        "-1",
        "-http_persistent",
        "1",
        "-http_multiple",
        "1",
        "-seg_max_retry",
        "1",
        "-rw_timeout",
        "3000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "408,429,5xx",
        "-reconnect_max_retries",
        "1",
        "-reconnect_delay_max",
        "1",
        "-reconnect_delay_total_max",
        "2",
        "-user_agent",
        USER_AGENT,
        "-headers",
        "Referer: https://live.douyin.com/\r\n",
        "-i",
        data.stream_url,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-f",
        "mpegts",
        "-y",
        str(output),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    log(f"started segment: {output}")


async def stop_recording(data: DouyinLiveData) -> None:
    process = data.ffmpeg_process
    if process is None:
        return

    if process.returncode is None:
        log("stopping FFmpeg")
        stdin = process.stdin
        if stdin is not None:
            try:
                stdin.write(b"q")
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            stdin.close()
            try:
                await stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

        try:
            await asyncio.wait_for(process.wait(), timeout=15.0)
        except TimeoutError:
            log("FFmpeg did not stop after q; sending SIGTERM")
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            log("FFmpeg did not stop after SIGTERM; sending SIGKILL")
            try:
                process.kill()
            except ProcessLookupError:
                pass

    await process.wait()
    log(f"FFmpeg exited with code {process.returncode}")

    directory = data.recording_directory
    if directory is not None:
        for path in directory.glob("part-*.ts"):
            if path.stat().st_size == 0:
                path.unlink()
                log(f"removed empty segment: {path}")

    data.ffmpeg_process = None


async def finish_session(data: DouyinLiveData) -> None:
    await stop_recording(data)
    directory = data.recording_directory
    if directory is None:
        log("no recording session to finalize")
        return

    segments = []
    for path in sorted(directory.glob("part-*.ts")):
        if path.stat().st_size == 0:
            continue

        probe = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await probe.communicate()
        stream_types = set(stdout.decode().splitlines())
        if probe.returncode == 0 and {"video", "audio"} <= stream_types:
            segments.append(path)
        else:
            log(
                f"skipped invalid segment: {path} "
                f"(ffprobe={probe.returncode}, streams={sorted(stream_types)})"
            )

    if segments:
        concat = directory / "segments.ffconcat"
        lines = ["ffconcat version 1.0"]
        lines.extend(f"file '{segment.name}'" for segment in segments)
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
        output = directory / "recording.mkv"
        log(f"merging {len(segments)} segments into {output}")

        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat.name,
            "-map",
            "0",
            "-c",
            "copy",
            "-y",
            "recording.mkv",
            cwd=directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        returncode = await process.wait()
        if returncode != 0:
            log(f"merge failed with exit code {returncode}")
            raise RuntimeError(f"FFmpeg merge failed with exit code {returncode}")
        log(f"merge completed: {output}")
    else:
        log("no valid segments to merge")


async def manage_recording(data: DouyinLiveData) -> None:
    try:
        while not data.stop_event.is_set():
            await wait(data, 1.0)
            if data.stop_event.is_set():
                break

            if data.stream_url is None:
                await stop_recording(data)
                continue

            process = data.ffmpeg_process
            if process is None:
                await start_recording(data)
                continue

            if process.returncode is not None:
                await stop_recording(data)

    finally:
        await finish_session(data)

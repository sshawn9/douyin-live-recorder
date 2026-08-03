from __future__ import annotations

import asyncio
from pathlib import Path

from .data import DouyinLiveData


def parse_segment_filename(path: Path) -> tuple[int, int]:
    _, playlist_index, sequence = path.stem.split("-", 2)
    return int(playlist_index), int(sequence)


async def merge_segments(data: DouyinLiveData) -> None:
    runtime_dir = data.runtime_dir
    segment_dir = data.segment_dir
    assert runtime_dir is not None
    assert segment_dir is not None
    data.status = "Preparing merging"

    for partial_path in segment_dir.glob("segment-*.part"):
        partial_path.unlink(missing_ok=True)

    segment_paths = sorted(
        segment_dir.glob("segment-*.ts"),
        key=parse_segment_filename,
    )
    if not segment_paths:
        data.status = "Finished with no segments"
        data.log(
            f"merge skipped: downloaded_segments=0 "
            f"playlists={len(data.playlists)} failed_segments={len(data.failed)}"
        )
        return

    concat_path = runtime_dir / "segments.ffconcat"
    concat_path.write_text(
        "ffconcat version 1.0\n"
        + "".join(
            f"file '{segment_path.relative_to(runtime_dir).as_posix()}'\n"
            for segment_path in segment_paths
        ),
        encoding="utf-8",
    )
    output_path = runtime_dir / "recording.mkv"
    temporary_path = runtime_dir / "recording.partial.mkv"
    temporary_path.unlink(missing_ok=True)
    data.status = "Merging segments"

    ffmpeg = await asyncio.create_subprocess_exec(
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
        concat_path.name,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-y",
        temporary_path.name,
        cwd=runtime_dir,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    returncode = await ffmpeg.wait()
    if returncode != 0 or not temporary_path.is_file() or temporary_path.stat().st_size == 0:
        temporary_path.unlink(missing_ok=True)
        data.status = "Merge failed"
        data.log(
            f"merge failed: returncode={returncode} segments={len(segment_paths)} "
            "output_valid=false source_segments_preserved=true"
        )
        return

    temporary_path.replace(output_path)
    data.status = "Finished"
    data.log(f"merge completed: segments={len(segment_paths)} output={str(output_path)!r}")

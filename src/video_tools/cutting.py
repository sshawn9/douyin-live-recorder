from __future__ import annotations

import math
import subprocess
import tempfile
from bisect import bisect_left, bisect_right
from pathlib import Path

import typer

from .ffmpeg import (
    executable,
    format_duration,
    input_path,
    output_path,
    probe_duration,
    probe_media,
    probe_start_time,
    run_ffmpeg,
)


def _parse_time(value: str, option: str) -> float:
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3 or any(not part for part in parts):
        raise typer.BadParameter(f"invalid time for {option}: {value}")
    try:
        if len(parts) == 1:
            seconds = float(parts[0])
        elif len(parts) == 2:
            minutes = int(parts[0])
            second_part = float(parts[1])
            if not 0 <= second_part < 60:
                raise ValueError
            seconds = minutes * 60 + second_part
        else:
            hours = int(parts[0])
            minutes = int(parts[1])
            second_part = float(parts[2])
            if not 0 <= minutes < 60 or not 0 <= second_part < 60:
                raise ValueError
            seconds = hours * 3600 + minutes * 60 + second_part
    except ValueError as error:
        raise typer.BadParameter(f"invalid time for {option}: {value}") from error
    if not math.isfinite(seconds) or seconds < 0:
        raise typer.BadParameter(f"invalid time for {option}: {value}")
    return seconds


def _scan_duration(path: Path) -> float | None:
    process = subprocess.Popen(
        [
            executable("ffmpeg"),
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-v",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            str(path),
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    duration = 0.0
    if process.stdout is not None:
        for line in process.stdout:
            if not line.startswith("out_time_us="):
                continue
            try:
                duration = max(duration, int(line.removeprefix("out_time_us=")) / 1_000_000)
            except ValueError:
                continue
    if process.wait() != 0 or duration <= 0:
        return None
    return duration


def _cut_ranges(
    duration: float,
    remove_start: str | None,
    remove_end: str | None,
    remove: list[str],
) -> list[tuple[float, float]]:
    removed: list[tuple[float, float]] = []
    if remove_start is not None:
        length = _parse_time(remove_start, "--remove-start")
        if length > duration:
            raise typer.BadParameter("--remove-start exceeds the media duration")
        if length:
            removed.append((0.0, length))

    if remove_end is not None:
        length = _parse_time(remove_end, "--remove-end")
        if length > duration:
            raise typer.BadParameter("--remove-end exceeds the media duration")
        if length:
            removed.append((duration - length, duration))

    for value in remove:
        if value.count("..") != 1:
            raise typer.BadParameter(f"invalid --remove range: {value}; expected START..END")
        start_text, end_text = value.split("..")
        start = _parse_time(start_text, "--remove")
        end = _parse_time(end_text, "--remove")
        if start >= end:
            raise typer.BadParameter(f"invalid --remove range: {value}; START must precede END")
        if end > duration:
            raise typer.BadParameter(f"--remove range exceeds the media duration: {value}")
        removed.append((start, end))

    if not removed:
        raise typer.BadParameter("at least one removal option is required")

    merged: list[tuple[float, float]] = []
    for start, end in sorted(removed):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    if merged == [(0.0, duration)]:
        raise typer.BadParameter("the removal ranges cover the complete media file")
    return merged


def _keyframe_times(path: Path) -> list[float]:
    process = subprocess.run(
        [
            executable("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=pts_time,dts_time,flags",
            "-of",
            "compact=p=0:nk=0",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        message = process.stderr.strip() or f"ffprobe exited with code {process.returncode}"
        raise RuntimeError(message)

    keyframes: list[float] = []
    for line in process.stdout.splitlines():
        fields = dict(field.split("=", 1) for field in line.split("|") if "=" in field)
        if "K" not in fields.get("flags", ""):
            continue
        for name in ("dts_time", "pts_time"):
            try:
                timestamp = float(fields[name])
            except KeyError, ValueError:
                continue
            if math.isfinite(timestamp):
                keyframes.append(timestamp)
                break

    if not keyframes:
        raise typer.BadParameter("the primary video stream contains no usable keyframes")
    return sorted(set(keyframes))


def _align_cut_ranges(
    removed: list[tuple[float, float]],
    duration: float,
    keyframes: list[float],
    start_time: float,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    relative_keyframes = [timestamp - start_time for timestamp in keyframes]
    aligned: list[tuple[float, float]] = []
    for start, end in removed:
        if start <= 0:
            aligned_start = 0.0
        else:
            index = bisect_right(relative_keyframes, start + 1e-6) - 1
            aligned_start = max(0.0, relative_keyframes[index]) if index >= 0 else 0.0

        if end >= duration:
            aligned_end = duration
        else:
            index = bisect_left(relative_keyframes, end - 1e-6)
            aligned_end = (
                min(duration, relative_keyframes[index])
                if index < len(relative_keyframes)
                else duration
            )
        aligned.append((aligned_start, aligned_end))

    merged: list[tuple[float, float]] = []
    for start, end in sorted(aligned):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    kept: list[tuple[float, float]] = []
    position = 0.0
    for start, end in merged:
        if start > position:
            kept.append((position, start))
        position = end
    if position < duration:
        kept.append((position, duration))
    if not kept:
        raise typer.BadParameter(
            "keyframe alignment makes the removal ranges cover the complete media file"
        )
    return merged, kept


def cut_video(
    input_file: Path,
    output_file: Path | None,
    remove_start: str | None,
    remove_end: str | None,
    remove: list[str],
    overwrite: bool,
) -> None:
    path = input_path(input_file)
    if output_file is None:
        suffix = path.suffix or ".mkv"
        output_file = path.with_name(f"{path.stem}.cut{suffix}")
    destination = output_path(output_file, [path], overwrite)
    data, _ = probe_media(path)
    duration = probe_duration(data)
    if duration is None:
        typer.echo("Container duration unavailable; scanning all packet timestamps...")
        duration = _scan_duration(path)
    if duration is None:
        raise typer.BadParameter("media duration could not be determined")
    start_time = probe_start_time(data)
    requested_removed = _cut_ranges(duration, remove_start, remove_end, remove)
    typer.echo("Finding video keyframes...")
    keyframes = _keyframe_times(path)
    removed, kept = _align_cut_ranges(requested_removed, duration, keyframes, start_time)
    input_ranges = [
        (
            keyframes[0] if start <= 1e-6 else start_time + start,
            start_time + end,
        )
        for start, end in kept
    ]

    typer.echo("Cut")
    typer.echo(f"  Input: {path}")
    typer.echo(f"  Output: {destination}")
    for start, end in requested_removed:
        typer.echo(f"  Requested remove: {format_duration(start)} .. {format_duration(end)}")
    for start, end in removed:
        typer.echo(f"  Keyframe-aligned remove: {format_duration(start)} .. {format_duration(end)}")
    typer.echo(
        f"  Expected duration: {format_duration(sum(end - start for start, end in input_ranges))}"
    )
    typer.echo("  Video and audio will be copied without re-encoding")

    concat_path: Path | None = None
    temporary_path: Path | None = None
    try:
        escaped_path = str(path).replace("'", "'\\''")
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".video-tools-cut-",
            suffix=".ffconcat",
            dir=destination.parent,
            delete=False,
        ) as concat_file:
            concat_path = Path(concat_file.name)
            concat_file.write("ffconcat version 1.0\n")
            for input_start, input_end in input_ranges:
                concat_file.write(f"file '{escaped_path}'\n")
                concat_file.write(f"inpoint {input_start:.6f}\n")
                concat_file.write(f"outpoint {input_end:.6f}\n")
                concat_file.write(f"duration {input_end - input_start:.6f}\n")

        suffix = destination.suffix or ".mkv"
        with tempfile.NamedTemporaryFile(
            prefix=".video-tools-cut-",
            suffix=suffix,
            dir=destination.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

        run_ffmpeg(
            [
                "-hide_banner",
                "-nostdin",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-map",
                "0",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                "-y",
                str(temporary_path),
            ]
        )
        temporary_path.replace(destination)
        typer.echo(f"  Completed: {destination}")
    finally:
        if concat_path is not None:
            concat_path.unlink(missing_ok=True)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

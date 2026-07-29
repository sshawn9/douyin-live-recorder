from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

import typer


def executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} was not found in PATH")
    return path


def input_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise typer.BadParameter(f"input file does not exist: {path}")
    return path


def output_path(path: Path, input_paths: list[Path], overwrite: bool) -> Path:
    path = path.expanduser().resolve()
    if path in input_paths:
        raise typer.BadParameter("output must be different from every input")
    if path.exists() and not overwrite:
        raise typer.BadParameter(f"output already exists: {path}; use --overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def run_ffmpeg(arguments: list[str]) -> None:
    process = subprocess.run([executable("ffmpeg"), *arguments], check=False)
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg exited with code {process.returncode}")


def probe_media(path: Path) -> tuple[dict[str, Any], list[str]]:
    process = subprocess.run(
        [
            executable("ffprobe"),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        message = process.stderr.strip() or f"ffprobe exited with code {process.returncode}"
        raise RuntimeError(message)

    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"ffprobe returned invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise RuntimeError("ffprobe returned an unexpected result")
    errors = [line.strip() for line in process.stderr.splitlines() if line.strip()]
    return data, errors


def format_duration(value: object) -> str:
    try:
        seconds = float(str(value))
    except TypeError, ValueError:
        return "unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}"


def probe_duration(data: dict[str, Any]) -> float | None:
    values: list[object] = []
    format_data = data.get("format")
    if isinstance(format_data, dict):
        values.append(format_data.get("duration"))
    streams = data.get("streams")
    if isinstance(streams, list):
        values.extend(stream.get("duration") for stream in streams if isinstance(stream, dict))

    durations: list[float] = []
    for value in values:
        try:
            duration = float(str(value))
        except TypeError, ValueError:
            continue
        if math.isfinite(duration) and duration > 0:
            durations.append(duration)
    return max(durations) if durations else None


def probe_start_time(data: dict[str, Any]) -> float:
    format_data = data.get("format")
    if not isinstance(format_data, dict):
        return 0.0
    try:
        start_time = float(str(format_data.get("start_time")))
    except TypeError, ValueError:
        return 0.0
    return start_time if math.isfinite(start_time) else 0.0

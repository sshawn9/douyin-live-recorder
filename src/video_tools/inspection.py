from __future__ import annotations

import subprocess
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Any

import typer

from .ffmpeg import executable, format_duration, input_path, probe_duration, probe_media


class CheckMode(StrEnum):
    QUICK = "quick"
    BALANCED = "balanced"
    FULL = "full"


def _size(value: object) -> str:
    try:
        size = int(str(value))
    except TypeError, ValueError:
        return "unknown"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    number = float(size)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.2f} {unit}"
        number /= 1024
    return str(size)


def _bit_rate(value: object) -> str:
    try:
        bit_rate = int(str(value))
    except TypeError, ValueError:
        return "unknown"
    if bit_rate >= 1_000_000:
        return f"{bit_rate / 1_000_000:.2f} Mb/s"
    if bit_rate >= 1_000:
        return f"{bit_rate / 1_000:.2f} kb/s"
    return f"{bit_rate} b/s"


def _frame_rate(value: object) -> str:
    try:
        rate = float(Fraction(str(value)))
    except ValueError, ZeroDivisionError:
        return "unknown"
    return f"{rate:.3f}".rstrip("0").rstrip(".") + " fps"


def _sample_rate(value: object) -> str:
    try:
        rate = int(str(value))
    except TypeError, ValueError:
        return "unknown"
    return f"{rate / 1000:g} kHz"


def _codec(stream: dict[str, Any]) -> str:
    name = str(stream.get("codec_name") or "unknown")
    long_name = stream.get("codec_long_name")
    if long_name and str(long_name).casefold() != name.casefold():
        return f"{long_name} ({name})"
    return name


def _stream_label(stream: dict[str, Any]) -> str:
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return ""
    values = [str(tags[key]) for key in ("language", "title") if tags.get(key)]
    return ", ".join(values)


def _media_report(path: Path, data: dict[str, Any]) -> str:
    format_data = data.get("format")
    if not isinstance(format_data, dict):
        format_data = {}
    streams = data.get("streams")
    if not isinstance(streams, list):
        streams = []

    typed_streams = [stream for stream in streams if isinstance(stream, dict)]
    counts: dict[str, int] = {}
    for stream in typed_streams:
        stream_type = str(stream.get("codec_type") or "other")
        counts[stream_type] = counts.get(stream_type, 0) + 1

    format_name = str(format_data.get("format_name") or "unknown")
    format_long_name = format_data.get("format_long_name")
    container = f"{format_long_name} ({format_name})" if format_long_name else format_name
    stream_summary = (
        ", ".join(f"{count} {stream_type}" for stream_type, count in sorted(counts.items()))
        or "none"
    )
    lines = [
        "File",
        f"  Path: {path}",
        f"  Container: {container}",
        f"  Duration: {format_duration(format_data.get('duration'))}",
        f"  Size: {_size(format_data.get('size'))}",
        f"  Overall bit rate: {_bit_rate(format_data.get('bit_rate'))}",
        f"  Streams: {stream_summary}",
    ]

    for stream in typed_streams:
        index = stream.get("index", "?")
        stream_type = str(stream.get("codec_type") or "other")
        label = _stream_label(stream)
        title = f"{stream_type.capitalize()} stream #{index}"
        if label:
            title += f" ({label})"
        lines.extend(["", title, f"  Codec: {_codec(stream)}"])

        profile = stream.get("profile")
        if profile:
            lines.append(f"  Profile: {profile}")

        if stream_type == "video":
            width = stream.get("width")
            height = stream.get("height")
            if width and height:
                lines.append(f"  Resolution: {width}x{height}")
            if stream.get("pix_fmt"):
                lines.append(f"  Pixel format: {stream['pix_fmt']}")
            rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
            lines.append(f"  Frame rate: {_frame_rate(rate)}")
            color = ", ".join(
                str(stream[field])
                for field in (
                    "color_range",
                    "color_space",
                    "color_transfer",
                    "color_primaries",
                )
                if stream.get(field)
            )
            if color:
                lines.append(f"  Color: {color}")
        elif stream_type == "audio":
            sample_rate = stream.get("sample_rate")
            if sample_rate:
                lines.append(f"  Sample rate: {_sample_rate(sample_rate)}")
            channels = stream.get("channels")
            layout = stream.get("channel_layout")
            if channels:
                channel_text = f"{channels}"
                if layout:
                    channel_text += f" ({layout})"
                lines.append(f"  Channels: {channel_text}")

        if stream.get("bit_rate"):
            lines.append(f"  Bit rate: {_bit_rate(stream['bit_rate'])}")
        if stream.get("duration"):
            lines.append(f"  Duration: {format_duration(stream['duration'])}")

        disposition = stream.get("disposition")
        if isinstance(disposition, dict):
            flags = [name for name in ("default", "forced") if disposition.get(name)]
            if flags:
                lines.append(f"  Disposition: {', '.join(flags)}")

    return "\n".join(lines)


def _run_check(arguments: list[str]) -> tuple[int, int, list[str], float | None]:
    process = subprocess.Popen(
        [
            executable("ffmpeg"),
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-v",
            "error",
            "-stats_period",
            "86400",
            "-progress",
            "pipe:1",
            *arguments,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    error_count = 0
    errors: list[str] = []
    if process.stderr is not None:
        for line in process.stderr:
            line = line.strip()
            if not line:
                continue
            error_count += 1
            if len(errors) < 20:
                errors.append(line)
    actual_duration: float | None = None
    if process.stdout is not None:
        for line in process.stdout:
            if not line.startswith("out_time_us="):
                continue
            try:
                seconds = int(line.removeprefix("out_time_us=")) / 1_000_000
            except ValueError:
                continue
            if seconds >= 0:
                actual_duration = max(actual_duration or 0.0, seconds)
    return process.wait(), error_count, errors, actual_duration


def full_check(path: Path) -> tuple[int, int, list[str], float | None]:
    return _run_check(
        [
            "-i",
            str(path),
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-f",
            "null",
            "-",
        ]
    )


def packet_check(path: Path) -> tuple[int, int, list[str], float | None]:
    return _run_check(
        [
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
        ]
    )


def sample_intervals(duration: object) -> list[tuple[float, float]]:
    try:
        total = float(str(duration))
    except TypeError, ValueError:
        return [(0.0, 30.0)]
    if total <= 0:
        return [(0.0, 30.0)]
    if total <= 60:
        return [(0.0, total)]

    sample_duration = 5.0
    last_start = total - sample_duration
    return [(last_start * index / 11, sample_duration) for index in range(12)]


def sample_check(
    path: Path, intervals: list[tuple[float, float]]
) -> tuple[int, int, list[str], int]:
    returncode = 0
    error_count = 0
    errors: list[str] = []
    failed_intervals = 0
    for start, duration in intervals:
        sample_returncode, sample_error_count, sample_errors, _ = _run_check(
            [
                "-ss",
                f"{start:.3f}",
                "-i",
                str(path),
                "-t",
                f"{duration:.3f}",
                "-map",
                "0:v?",
                "-map",
                "0:a?",
                "-f",
                "null",
                "-",
            ]
        )
        error_count += sample_error_count
        if len(errors) < 20:
            errors.extend(sample_errors[: 20 - len(errors)])
        if sample_returncode != 0 or sample_error_count:
            failed_intervals += 1
        if sample_returncode != 0:
            returncode = sample_returncode
    return returncode, error_count, errors, failed_intervals


def show_errors(errors: list[str], error_count: int) -> None:
    for error in errors[:20]:
        typer.echo(f"    {error}")
    if error_count > min(len(errors), 20):
        typer.echo(f"    ... {error_count - min(len(errors), 20)} more error lines")


def show_timeline(data: dict[str, Any], actual_duration: float | None) -> str | None:
    if actual_duration is None:
        typer.echo("  Timeline: unavailable")
        return None

    declared_duration = probe_duration(data)
    if declared_duration is None:
        message = (
            "container duration is unavailable; "
            f"actual packet span is {format_duration(actual_duration)}"
        )
        typer.echo(f"  Timeline: mismatch ({message})")
        return message

    difference = declared_duration - actual_duration
    if abs(difference) <= 1.0:
        typer.echo(f"  Timeline: OK ({format_duration(actual_duration)})")
        return None

    direction = "longer" if difference > 0 else "shorter"
    message = (
        f"declared {format_duration(declared_duration)}, "
        f"actual {format_duration(actual_duration)}; declared duration is "
        f"{format_duration(abs(difference))} {direction}"
    )
    typer.echo(f"  Timeline: mismatch ({message})")
    return message


def show_info(input_file: Path, check: CheckMode) -> None:
    path = input_path(input_file)
    data, probe_errors = probe_media(path)
    typer.echo(_media_report(path, data))

    typer.echo("\nIntegrity check")
    typer.echo(f"  Mode: {check.value}")
    if check is CheckMode.QUICK:
        typer.echo("  Timeline: not checked in quick mode")
        if probe_errors:
            typer.echo(f"  Status: errors found during probe ({len(probe_errors)} lines)")
            show_errors(probe_errors, len(probe_errors))
            raise typer.Exit(2)
        typer.echo("  Status: no errors found by the basic probe")
        typer.echo("  Coverage: container headers and stream metadata only")
        return

    if check is CheckMode.FULL:
        typer.echo("  Decoding the complete video and audio streams...")
        returncode, decode_error_count, decode_errors, actual_duration = full_check(path)
        timeline_issue = show_timeline(data, actual_duration)
        error_count = len(probe_errors) + decode_error_count
        errors = [*probe_errors, *decode_errors][:20]
        if returncode == 0 and error_count == 0:
            if timeline_issue is not None:
                typer.echo("  Status: timeline mismatch")
                raise typer.Exit(2)
            typer.echo("  Status: OK")
            typer.echo("  Coverage: every decodable video and audio frame")
            return

        if returncode != 0:
            typer.echo(f"  Status: check failed (FFmpeg exit code {returncode})")
        else:
            typer.echo(f"  Status: errors found ({error_count} FFmpeg error lines)")
        show_errors(errors, error_count)
        raise typer.Exit(2)

    typer.echo("  Reading every compressed video and audio packet...")
    packet_returncode, packet_error_count, packet_errors, actual_duration = packet_check(path)
    timeline_issue = show_timeline(data, actual_duration)
    format_data = data.get("format")
    duration = format_data.get("duration") if isinstance(format_data, dict) else None
    intervals = sample_intervals(duration)
    typer.echo(
        f"  Decoding {len(intervals)} sampled interval(s), "
        f"{sum(length for _, length in intervals):.1f} seconds total..."
    )
    sample_returncode, sample_error_count, sample_errors, failed_intervals = sample_check(
        path, intervals
    )
    packet_clean = packet_returncode == 0 and packet_error_count == 0 and not probe_errors
    sample_ambiguous = sample_returncode != 0 or sample_error_count > 0
    if packet_clean and sample_ambiguous:
        if timeline_issue is not None:
            typer.echo("  Status: timeline mismatch")
            typer.echo(
                "  Note: sampled seek errors were inconclusive and were not treated as corruption"
            )
            raise typer.Exit(2)
        typer.echo("  Status: inconclusive")
        typer.echo(
            "  Reason: sampled decoding reported errors after seeking, "
            "but the complete packet scan was clean"
        )
        show_errors(sample_errors, sample_error_count)
        typer.echo("  Run again with --check full for a conclusive result")
        raise typer.Exit(3)

    error_count = len(probe_errors) + packet_error_count + sample_error_count
    errors = [*probe_errors, *packet_errors, *sample_errors][:20]
    if packet_returncode == 0 and sample_returncode == 0 and error_count == 0:
        if timeline_issue is not None:
            typer.echo("  Status: timeline mismatch")
            raise typer.Exit(2)
        typer.echo("  Status: no errors found")
        typer.echo("  Coverage: all packets read; selected intervals decoded")
        return

    typer.echo(
        "  Status: errors found "
        f"(packet scan exit {packet_returncode}, {failed_intervals} sampled intervals failed)"
    )
    show_errors(errors, error_count)
    raise typer.Exit(2)

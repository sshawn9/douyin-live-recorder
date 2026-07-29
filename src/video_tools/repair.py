from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import typer

from .ffmpeg import executable, input_path, output_path, probe_media
from .inspection import (
    CheckMode,
    full_check,
    packet_check,
    sample_check,
    sample_intervals,
    show_errors,
    show_timeline,
)


def repair_video(
    input_file: Path,
    output_file: Path | None,
    check: CheckMode,
    overwrite: bool,
) -> None:
    path = input_path(input_file)
    if output_file is None:
        suffix = path.suffix or ".mkv"
        output_file = path.with_name(f"{path.stem}.repaired{suffix}")
    destination = output_path(output_file, [path], overwrite)
    suffix = destination.suffix or ".mkv"
    with tempfile.NamedTemporaryFile(
        prefix=".video-tools-repair-",
        suffix=suffix,
        dir=destination.parent,
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)

    try:
        typer.echo("Repair")
        typer.echo(f"  Input: {path}")
        typer.echo(f"  Output: {destination}")
        typer.echo("  Copying valid packets and discarding packets marked as corrupt...")
        process = subprocess.run(
            [
                executable("ffmpeg"),
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "warning",
                "-fflags",
                "+discardcorrupt+genpts",
                "-i",
                str(path),
                "-map",
                "0:v?",
                "-map",
                "0:a?",
                "-map",
                "0:s?",
                "-map",
                "0:t?",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                "-y",
                str(temporary_path),
            ],
            check=False,
        )
        if process.returncode != 0:
            typer.echo(f"  Status: failed (FFmpeg exit code {process.returncode})")
            raise typer.Exit(1)

        data, probe_errors = probe_media(temporary_path)
        streams = data.get("streams")
        if not isinstance(streams, list) or not any(
            isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams
        ):
            typer.echo("  Status: failed (the output contains no video stream)")
            raise typer.Exit(1)

        format_data = data.get("format")
        duration = format_data.get("duration") if isinstance(format_data, dict) else None
        errors = probe_errors[:20]
        error_count = len(probe_errors)
        failed = False
        inconclusive = False
        sample_error_count = 0
        sample_errors: list[str] = []
        timeline_issue: str | None = None

        typer.echo(f"  Validating repaired output with {check.value} check...")
        if check is CheckMode.FULL:
            returncode, check_error_count, check_errors, actual_duration = full_check(
                temporary_path
            )
            timeline_issue = show_timeline(data, actual_duration)
            failed = returncode != 0
        elif check is CheckMode.BALANCED:
            (
                packet_returncode,
                packet_error_count,
                packet_errors,
                actual_duration,
            ) = packet_check(temporary_path)
            timeline_issue = show_timeline(data, actual_duration)
            intervals = sample_intervals(duration)
            sample_returncode, sample_error_count, sample_errors, _ = sample_check(
                temporary_path, intervals
            )
            packet_clean = packet_returncode == 0 and packet_error_count == 0 and not probe_errors
            sample_ambiguous = sample_returncode != 0 or sample_error_count > 0
            if packet_clean and sample_ambiguous:
                returncode = 0
                check_error_count = 0
                check_errors = []
                inconclusive = timeline_issue is None
            else:
                returncode = packet_returncode or sample_returncode
                check_error_count = packet_error_count + sample_error_count
                check_errors = [*packet_errors, *sample_errors][:20]
            failed = returncode != 0
        else:
            check_error_count = 0
            check_errors = []

        error_count += check_error_count
        if len(errors) < 20:
            errors.extend(check_errors[: 20 - len(errors)])
        temporary_path.replace(destination)

        if inconclusive:
            typer.echo("  Status: inconclusive")
            typer.echo(
                "  Reason: sampled decoding reported errors after seeking, "
                "but the complete packet scan was clean"
            )
            show_errors(sample_errors, sample_error_count)
            typer.echo(f"  Salvaged file retained at: {destination}")
            typer.echo("  Run info with --check full for a conclusive result")
            raise typer.Exit(3)

        if failed or error_count or timeline_issue is not None:
            if timeline_issue is not None and not failed and error_count == 0:
                typer.echo("  Status: partial (timeline mismatch)")
                typer.echo(f"  Salvaged file retained at: {destination}")
                raise typer.Exit(2)
            typer.echo(f"  Status: partial ({error_count} error lines remain)")
            show_errors(errors, error_count)
            typer.echo(f"  Salvaged file retained at: {destination}")
            raise typer.Exit(2)

        typer.echo("  Status: repaired")
        typer.echo(f"  Repaired file: {destination}")
    finally:
        temporary_path.unlink(missing_ok=True)

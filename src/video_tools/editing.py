from __future__ import annotations

import tempfile
from pathlib import Path

import typer

from .ffmpeg import input_path, output_path, run_ffmpeg


def concatenate_videos(input_files: list[Path], output_file: Path, overwrite: bool) -> None:
    if len(input_files) < 2:
        raise typer.BadParameter("concat requires at least two input files")

    input_paths = [input_path(path) for path in input_files]
    destination = output_path(output_file, input_paths, overwrite)
    concat_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".video-tools-",
            suffix=".ffconcat",
            dir=destination.parent,
            delete=False,
        ) as concat_file:
            concat_path = Path(concat_file.name)
            concat_file.write("ffconcat version 1.0\n")
            for path in input_paths:
                escaped_path = str(path).replace("'", "'\\''")
                concat_file.write(f"file '{escaped_path}'\n")

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
                "-y" if overwrite else "-n",
                str(destination),
            ]
        )
    finally:
        if concat_path is not None:
            concat_path.unlink(missing_ok=True)


def compact_video(input_file: Path, output_file: Path, overwrite: bool) -> None:
    path = input_path(input_file)
    destination = output_path(output_file, [path], overwrite)
    run_ffmpeg(
        [
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "-1",
            "-c",
            "copy",
            "-y" if overwrite else "-n",
            str(destination),
        ]
    )

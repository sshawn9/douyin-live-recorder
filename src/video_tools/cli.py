from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .cutting import cut_video
from .editing import compact_video, concatenate_videos
from .inspection import CheckMode, show_info
from .repair import repair_video

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command("info")
def info_command(
    input_file: Annotated[Path, typer.Argument(help="Video file to inspect")],
    check: Annotated[
        CheckMode,
        typer.Option(help="Integrity check depth"),
    ] = CheckMode.BALANCED,
) -> None:
    """Show a media summary and run a selectable integrity check."""
    show_info(input_file, check)


@app.command("repair")
def repair_command(
    input_file: Annotated[Path, typer.Argument(help="Damaged source video")],
    output_file: Annotated[
        Path | None,
        typer.Argument(help="Repaired output video; generated when omitted"),
    ] = None,
    check: Annotated[
        CheckMode,
        typer.Option(help="Validation depth for the repaired file"),
    ] = CheckMode.BALANCED,
    overwrite: Annotated[bool, typer.Option(help="Replace an existing output")] = False,
) -> None:
    """Salvage valid packets without re-encoding and validate the result."""
    repair_video(input_file, output_file, check, overwrite)


@app.command("cut")
def cut_command(
    input_file: Annotated[Path, typer.Argument(help="Source video")],
    output_file: Annotated[
        Path | None,
        typer.Argument(help="Output video; generated when omitted"),
    ] = None,
    remove_start: Annotated[
        str | None,
        typer.Option(help="Duration to remove from the beginning"),
    ] = None,
    remove_end: Annotated[
        str | None,
        typer.Option(help="Duration to remove from the end"),
    ] = None,
    remove: Annotated[
        list[str] | None,
        typer.Option(help="Original timeline range START..END; repeatable"),
    ] = None,
    overwrite: Annotated[bool, typer.Option(help="Replace an existing output")] = False,
) -> None:
    """Remove one or more ranges without re-encoding."""
    cut_video(
        input_file,
        output_file,
        remove_start,
        remove_end,
        remove or [],
        overwrite,
    )


@app.command("concat")
def concat_command(
    input_files: Annotated[list[Path], typer.Argument(help="Videos in output order")],
    output_file: Annotated[Path, typer.Option("--output", "-o", help="Output video")],
    overwrite: Annotated[bool, typer.Option(help="Replace an existing output")] = False,
) -> None:
    """Join compatible videos without re-encoding."""
    concatenate_videos(input_files, output_file, overwrite)


@app.command("compact")
def compact_command(
    input_file: Annotated[Path, typer.Argument(help="Source video")],
    output_file: Annotated[Path, typer.Argument(help="Output video")],
    overwrite: Annotated[bool, typer.Option(help="Replace an existing output")] = False,
) -> None:
    """Keep the first video and audio streams without re-encoding."""
    compact_video(input_file, output_file, overwrite)


def main() -> None:
    try:
        app(prog_name="video-tools")
    except RuntimeError as error:
        typer.echo(str(error), err=True)
        raise SystemExit(1) from None

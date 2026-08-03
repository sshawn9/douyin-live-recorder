from __future__ import annotations

import asyncio
import shutil
from typing import Annotated

import typer

from .data import DouyinLiveData
from .run import start

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def record(
    user_id: Annotated[str, typer.Argument(help="Douyin user ID")],
) -> None:
    if shutil.which("ffmpeg") is None:
        typer.echo("FFmpeg was not found in PATH", err=True)
        raise typer.Exit(1)

    data = DouyinLiveData(
        user_id=user_id,
    )

    try:
        asyncio.run(start(data))
    except RuntimeError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error


def main() -> None:
    app(prog_name="douyin-live-recorder")

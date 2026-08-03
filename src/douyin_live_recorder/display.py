from __future__ import annotations

import asyncio

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .data import DouyinLiveData


def render_status(data: DouyinLiveData) -> RenderableType:
    tables: list[RenderableType] = []

    for index, playlist in enumerate(data.playlists, start=1):
        table = Table(title=f"Playlist {index}", expand=True)
        table.add_column("Min Seq")
        table.add_column("Max Seq")
        table.add_column("Missing")
        table.add_row(
            str(playlist.min_seq),
            str(playlist.max_seq),
            str(len(playlist.missing_seqs)),
        )
        tables.append(table)

    details = Table.grid(expand=True, padding=(0, 2))
    details.add_column(style="bold cyan", no_wrap=True)
    details.add_column(ratio=1, overflow="fold")
    details.add_row("Status", data.status)
    details.add_row("Session", str(data.runtime_dir or "-"))

    stats = Table(expand=True)
    stats.add_column("User")
    stats.add_column("Playlists")
    stats.add_column("Download Queue")
    stats.add_column("Retry Queue")
    stats.add_column("Failed")
    stats.add_row(
        data.user_id,
        str(len(data.playlists)),
        str(data.segment_jobs.qsize()),
        str(data.retry_jobs.qsize()),
        str(len(data.failed)),
    )

    overview = Panel(
        Group(details, stats),
        title="Overview",
        border_style="cyan",
    )

    return Group(*tables, overview)


async def display(data: DouyinLiveData) -> None:
    console = Console()
    if not console.is_terminal:
        return

    with Live(
        render_status(data),
        console=console,
        auto_refresh=False,
        transient=False,
    ) as live:
        try:
            while True:
                live.update(render_status(data), refresh=True)
                await asyncio.sleep(0.5)
        finally:
            live.update(render_status(data), refresh=True)

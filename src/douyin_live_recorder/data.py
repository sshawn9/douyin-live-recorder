from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


@dataclass
class DouyinLiveData:
    user_id: str
    output_dir: Path = Path("recordings")
    stream_url: str | None = None
    ffmpeg_process: asyncio.subprocess.Process | None = None
    recording_directory: Path | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


class ValidatedSegment(Protocol):
    media_sequence: int
    duration: float
    absolute_uri: str
    discontinuity: bool | None


@dataclass(slots=True)
class Segment:
    playlist_index: int
    sequence: int
    duration: float
    uri: str
    path: Path
    discontinuity: bool
    attempts: int = 0
    failed_at: float | None = None
    bytes_written: int = 0


@dataclass(slots=True)
class Playlist:
    media_url: str
    # segments: list[Segment] = field(default_factory=list)
    min_seq: int | None = None
    max_seq: int | None = None
    missing_seqs: set[int] = field(default_factory=set)


@dataclass
class DouyinLiveData:
    user_id: str
    recordings_dir: Path = Path("recordings")
    user_dir: Path | None = None
    runtime_dir: Path | None = None
    segment_dir: Path | None = None
    playlists: list[Playlist] = field(default_factory=list)
    failed: list[Segment] = field(default_factory=list)
    segment_jobs: asyncio.Queue[Segment] = field(default_factory=asyncio.Queue)
    retry_jobs: asyncio.Queue[Segment] = field(default_factory=asyncio.Queue)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "Starting"

    def log(self, message: str) -> None:
        assert self.runtime_dir is not None
        now = datetime.now(UTC).astimezone()
        with (self.runtime_dir / "runtime.log").open("a", encoding="utf-8") as log_file:
            log_file.write(f"{now:%H:%M:%S} {message}\n")

    def __del__(self) -> None:
        try:
            if self.runtime_dir is None or not self.runtime_dir.is_dir():
                return

            lines = [
                "final state:",
                f"  user_id: {self.user_id}",
                f"  runtime_dir: {self.runtime_dir}",
                f"  segment_dir: {self.segment_dir}",
                f"  status: {self.status}",
                f"  stop_requested: {self.stop_event.is_set()}",
                f"  queued_downloads: {self.segment_jobs.qsize()}",
                f"  queued_retries: {self.retry_jobs.qsize()}",
                f"  playlists: {len(self.playlists)}",
            ]
            for index, playlist in enumerate(self.playlists, start=1):
                missing = ",".join(map(str, sorted(playlist.missing_seqs))) or "none"
                lines.extend(
                    (
                        f"    playlist {index}:",
                        f"      media_url: {playlist.media_url}",
                        f"      min_sequence: {playlist.min_seq}",
                        f"      max_sequence: {playlist.max_seq}",
                        f"      unobserved_sequences: {missing}",
                    )
                )

            lines.append(f"  failed_segments: {len(self.failed)}")
            for segment in self.failed:
                lines.extend(
                    (
                        f"    playlist={segment.playlist_index} sequence={segment.sequence}:",
                        f"      duration: {segment.duration}s",
                        f"      attempts: {segment.attempts}",
                        f"      discontinuity: {segment.discontinuity}",
                        f"      bytes_written: {segment.bytes_written}",
                        f"      uri: {segment.uri}",
                        f"      path: {segment.path}",
                    )
                )

            self.log("\n".join(lines))
        except (AttributeError, OSError, RuntimeError):
            return


async def wait_or_stop(data: DouyinLiveData, timeout: float) -> None:
    timeout *= random.uniform(0.9, 1.1)
    try:
        await asyncio.wait_for(data.stop_event.wait(), timeout=timeout)
    except TimeoutError:
        pass

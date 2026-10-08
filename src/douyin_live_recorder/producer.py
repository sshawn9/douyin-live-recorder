from __future__ import annotations

import time
from collections.abc import Sequence
from typing import cast

import httpx
import m3u8

from .data import (
    USER_AGENT,
    DouyinLiveData,
    Playlist,
    Segment,
    ValidatedSegment,
    wait_or_stop,
)
from .douyin import resolve_stream_url


async def produce_playlists(data: DouyinLiveData) -> None:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://live.douyin.com/",
    }
    page_client = httpx.AsyncClient(
        headers=headers,
        timeout=15.0,
        follow_redirects=True,
    )
    playlist_client = httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(5.0, connect=5.0),
        follow_redirects=True,
    )
    async with page_client, playlist_client:
        while not data.stop_event.is_set():
            stream_url = await resolve_stream_url(data, page_client)
            if stream_url is None:
                break

            data.status = "Resolving HLS playlist"
            try:
                media_url = await fetch_playlist_url(
                    playlist_client,
                    stream_url,
                )
            except (httpx.HTTPError, ValueError, m3u8.ParseError) as error:
                data.status = "HLS entry unavailable; retrying"
                data.log(
                    f"HLS entry unavailable: url={stream_url!r} "
                    f"error={type(error).__name__}({error})"
                )
                await wait_or_stop(data, 0.1)
                continue

            playlist = Playlist(
                media_url=media_url,
            )
            data.playlists.append(playlist)
            data.status = f"Recording playlist {len(data.playlists)}"
            data.log(f"HLS playlist started: index={len(data.playlists)}")
            await produce_segments(data, playlist_client)


async def produce_segments(
    data: DouyinLiveData,
    client: httpx.AsyncClient,
) -> None:
    playlist = data.playlists[-1]
    failures = 0
    last_progress: float | None = None

    while not data.stop_event.is_set():
        try:
            snapshot = await fetch_playlist_snapshot(client, playlist.media_url)
            segments = validate_segments(snapshot)
        except (httpx.HTTPError, ValueError, m3u8.ParseError) as error:
            failures += 1
            data.status = f"Retrying HLS playlist {len(data.playlists)} ({failures}/3)"
            data.log(
                f"media playlist request failed: {type(error).__name__}: {error}; failures: {failures}"
            )
            if failures >= 3:
                data.status = "Refreshing live stream"
                data.log(
                    f"HLS playlist unavailable: playlist={len(data.playlists)} "
                    f"consecutive_failures={failures} "
                    f"error={type(error).__name__}({error})"
                )
                return
            await wait_or_stop(data, 0.1)
            continue
        failures = 0
        data.status = f"Recording playlist {len(data.playlists)}"

        min_seq = segments[0].media_sequence
        max_seq = segments[-1].media_sequence
        if playlist.min_seq is None:
            # initialize once for each playlist
            playlist.min_seq = min_seq
            playlist.max_seq = min_seq - 1
            last_progress = time.monotonic()
        assert playlist.max_seq is not None
        assert last_progress is not None
        prev_max_seq = playlist.max_seq

        if min_seq < prev_max_seq + 1:
            # some repeated, no untracked, maybe recoverable
            for segment in segments:
                if segment.media_sequence in playlist.missing_seqs:
                    enqueue_segment(data, segment)
                    playlist.missing_seqs.remove(segment.media_sequence)

        if min_seq > prev_max_seq + 1:
            # jump forward, some untracked, no recoverable
            playlist.missing_seqs.update(range(prev_max_seq + 1, min_seq))
            data.log(
                f"HLS sequence gap observed: playlist={len(data.playlists)} "
                f"sequences={prev_max_seq + 1}-{min_seq - 1}"
            )

        for segment in segments:
            if segment.media_sequence > prev_max_seq:
                enqueue_segment(data, segment)

        playlist.max_seq = max(playlist.max_seq, max_seq)

        if snapshot.is_endlist:
            data.status = "Refreshing live stream"
            data.log(
                f"HLS playlist ended: playlist={len(data.playlists)} "
                f"max_sequence={playlist.max_seq} "
                f"unobserved_sequences={len(playlist.missing_seqs)}"
            )
            return

        assert snapshot.target_duration is not None
        target_duration = float(snapshot.target_duration)
        interval = target_duration / 2.0
        if max_seq > prev_max_seq:
            last_progress = time.monotonic()
            interval = target_duration

        stall_duration = max(2.0, target_duration * 2.5)
        if time.monotonic() - last_progress >= stall_duration:
            data.status = "Refreshing live stream"
            data.log(
                f"HLS playlist stalled: playlist={len(data.playlists)} "
                f"max_sequence={playlist.max_seq} threshold={stall_duration:g}s "
                f"unobserved_sequences={len(playlist.missing_seqs)}"
            )
            return

        await wait_or_stop(data, interval)


async def fetch_playlist_url(client: httpx.AsyncClient, url: str) -> str:
    # On 2026-08-01, FULL_HD1 URLs from 118 live rooms discovered through
    # /categorynew/4_101 to 4_108 were fetched and classified. The result was
    # 87 direct media playlists, 31 master-to-media hops, and no nested masters.
    # A second probe found exactly one child in each of 33 master playlists;
    # average bandwidth, resolution, frame rate, and codecs were all absent.
    # FULL_HD1 already determines quality, so require one child, follow it once,
    # and reject a second master as an unsupported structure.

    response = await client.get(url)
    response.raise_for_status()
    resolved_url = str(response.url)
    snapshot = m3u8.loads(response.text, uri=resolved_url)
    if not snapshot.is_variant:
        return resolved_url

    variants = snapshot.playlists
    if len(variants) != 1:
        raise ValueError(f"unexpected HLS master variant count: {len(variants)}")
    return variants[0].absolute_uri


async def fetch_playlist_snapshot(client: httpx.AsyncClient, url: str) -> m3u8.M3U8:
    response = await client.get(url)
    response.raise_for_status()
    resolved_url = str(response.url)
    snapshot = m3u8.loads(response.text, uri=resolved_url)
    if snapshot.is_variant:
        raise ValueError("nested HLS master playlist")
    if snapshot.target_duration is None or not snapshot.segments:
        raise ValueError("empty HLS media playlist")
    return snapshot


def validate_segments(snapshot: m3u8.M3U8) -> Sequence[ValidatedSegment]:
    for segment in snapshot.segments:
        if segment.media_sequence is None or segment.duration is None or not segment.absolute_uri:
            raise ValueError("incomplete HLS segment metadata")
        if (
            segment.byterange is not None
            or segment.init_section is not None
            or (segment.key is not None and segment.key.method not in (None, "NONE"))
        ):
            raise ValueError("unsupported HLS segment format")
    return cast(Sequence[ValidatedSegment], snapshot.segments)


def enqueue_segment(
    data: DouyinLiveData,
    segment: ValidatedSegment,
) -> None:
    assert data.segment_dir is not None

    index = len(data.playlists)

    segment = Segment(
        playlist_index=index,
        sequence=segment.media_sequence,
        duration=segment.duration,
        uri=segment.absolute_uri,
        path=data.segment_dir / f"segment-{index:04d}-{segment.media_sequence:010d}.ts",
        discontinuity=bool(segment.discontinuity),
    )
    data.segment_jobs.put_nowait(segment)

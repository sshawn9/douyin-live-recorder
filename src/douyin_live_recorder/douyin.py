from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .data import DouyinLiveData, wait_or_stop


def parse_live_page(page: str) -> tuple[dict[str, Any], str]:
    push = "self.__pace_f.push("
    decoder = json.JSONDecoder()
    chunks = []
    position = 0

    while True:
        position = page.find(push, position)
        if position < 0:
            break
        item, length = decoder.raw_decode(page[position + len(push) :])
        position += len(push) + length
        if item[0] != 1:
            continue

        chunks.append(item[1])

    # Flight row IDs and script chunk boundaries can change between page renders.
    flight = "".join(chunks)
    for match in re.finditer(r'"state"\s*:\s*(?=\{)', flight):
        state, _ = decoder.raw_decode(flight, match.end())
        if isinstance(state, dict) and "roomStore" in state:
            return state, flight

    raise ValueError("live page state is absent")


def get_declared_stream_params(
    flight: str,
    reference: str,
    stream_url: str,
) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    marker = f"{reference[1:]}:T"
    record = flight.index(",", flight.index(marker)) + 1
    stream_data, _ = decoder.raw_decode(flight[record:])

    for lines in stream_data["data"].values():
        for line in lines.values():
            if line.get("hls") == stream_url:
                return json.loads(line["sdk_params"])
    raise ValueError("selected HLS URL is absent from stream_data")


async def resolve_stream_url(
    data: DouyinLiveData,
    client: httpx.AsyncClient,
) -> str | None:
    data.status = "Checking live status"
    while not data.stop_event.is_set():
        try:
            response = await client.get(f"https://live.douyin.com/{data.user_id}")
            if data.stop_event.is_set():
                return None
            response.raise_for_status()
            data.status = "Checking live status"
        except httpx.HTTPError as error:
            data.status = "Live page unavailable; retrying"
            data.log(f"live page request failed: {type(error).__name__}: {error}")
            await wait_or_stop(data, 5.0)
            continue

        try:
            state, flight = parse_live_page(response.text)
            room = state["roomStore"]["roomInfo"]["room"]
            is_live = room["status"] == 2
            quality = "FULL_HD1"
            stream_url = None
            stream: dict[str, Any] = {}
            if is_live:
                stream = state["cameraStore"]["mainCameraInfo"]["h265Stream"]
                hls = stream["hls_pull_url_map"]
                # A 2026-08-01 survey of 119 live rooms found only these keys,
                # ordered highest to lowest: FULL_HD1, HD1, SD2, SD1.
                # FULL_HD1 appeared in every room, but does not guarantee 1080p.
                # Revalidate by sampling /categorynew/4_101 through 4_108,
                # counting hls_pull_url_map keys and matching each URL to stream_data.
                stream_url = hls.get(quality)
        except (IndexError, KeyError, TypeError, ValueError) as error:
            data.status = "Live page data unavailable; retrying"
            data.log(f"live page state unavailable: {type(error).__name__}: {error}")
            await wait_or_stop(data, 60.0)
            continue

        if not is_live:
            data.status = "Waiting for live on"
            await wait_or_stop(data, 60.0)
            continue

        if not isinstance(stream_url, str) or not stream_url:
            data.status = "Waiting for HLS stream"
            data.log(f"HLS unavailable: quality={quality}")
            await wait_or_stop(data, 5.0)
            continue

        # live_core_sdk_data
        # └── pull_data
        #     └── stream_data (React Flight text reference)
        #         └── data
        #             └── uhd/origin/hd/sd/ld
        #                 └── main
        #                     ├── hls
        #                     └── sdk_params
        #                         ├── resolution
        #                         ├── VCodec
        #                         ├── fps
        #                         └── vbitrate
        try:
            params = get_declared_stream_params(
                flight,
                stream["live_core_sdk_data"]["pull_data"]["stream_data"],
                stream_url,
            )
        except (IndexError, KeyError, TypeError, ValueError) as error:
            data.log(
                f"HLS selected: quality={quality} declared_parameters=unavailable "
                f"error={type(error).__name__}({error})"
            )
        else:
            resolution = params.get("resolution") or "unknown"
            codec = params.get("VCodec") or "unknown"
            fps = params.get("fps") or "unknown"
            bitrate = params.get("vbitrate") or "unknown"
            data.log(
                f"HLS selected: quality={quality} declared_resolution={resolution} "
                f"declared_codec={codec} declared_fps={fps} "
                f"declared_bitrate={bitrate}"
            )
        return stream_url

    return None

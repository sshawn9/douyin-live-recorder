from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from .data import USER_AGENT, DouyinLiveData
from .runtime import wait


def log(message: str) -> None:
    now = datetime.now(UTC).astimezone()
    print(f"{now:%H:%M:%S} Monitor: {message}", flush=True)


async def monitor(data: DouyinLiveData) -> None:
    client = httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://live.douyin.com/",
        },
        timeout=15.0,
        follow_redirects=True,
    )
    async with client:
        while not data.stop_event.is_set():
            log("---")
            is_live = None
            is_normal = None
            stream_url = None
            try:
                response = await client.get(f"https://live.douyin.com/{data.user_id}")
                response.raise_for_status()
            except httpx.HTTPError as error:
                log(f"Live page request failed: {error}, retry after 5s")
                await wait(data, 5.0)
                continue

            try:
                push = "self.__pace_f.push("
                start = response.text.index(f'{push}[1,"c:')
                item, _ = json.JSONDecoder().raw_decode(response.text[start + len(push) :])
                state = json.loads(item[1][2:])[3]["state"]
                room = state["roomStore"]["roomInfo"]["room"]
                is_live = room["status"] == 2
                if is_live:
                    pk = state["pkStore"]
                    linker = room["linker_detail"]
                    is_normal = not any(
                        (
                            pk["isInPK"],
                            pk["isInPunish"],
                            pk["transformToPK"],
                            room["linker_map"],
                            linker["linker_play_modes"],
                            linker["function_type"],
                        )
                    )
                if is_normal:
                    stream_url = state["cameraStore"]["mainCameraInfo"]["h265Stream"][
                        "hls_pull_url"
                    ]
            except (IndexError, KeyError, TypeError, ValueError):
                log("Unsupported live page structure, retry after 60s")
                await wait(data, 60.0)
                continue

            data.stream_url = stream_url if isinstance(stream_url, str) and stream_url else None

            if not is_live:
                log("not live, retry after 60s")
                await wait(data, 60.0)
                continue

            log(f"live normal status: {is_normal}")
            log("stream available" if stream_url else "stream unavailable")
            await wait(data, 20.0)

        log("stop event is set")

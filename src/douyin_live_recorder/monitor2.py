from __future__ import annotations

from datetime import UTC, datetime

import httpx

from .data import USER_AGENT, DouyinLiveData
from .runtime import wait


def log(message: str) -> None:
    now = datetime.now(UTC).astimezone()
    print(f"{now:%H:%M:%S} Monitor2: {message}", flush=True)


async def monitor2(data: DouyinLiveData) -> None:
    client = httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://live.douyin.com/",
        },
        timeout=15.0,
        follow_redirects=True,
    )
    params = {
        "aid": "6383",
        "app_name": "douyin_web",
        "live_id": "1",
        "device_platform": "web",
        "language": "zh-CN",
        "enter_from": "link_share",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Linux x86_64",
        "browser_name": "Chrome",
        "browser_version": "138.0.0.0",
        "os_name": "Linux",
        "os_version": "x86_64",
        "web_rid": data.user_id,
        "enter_source": "",
        "insert_task_id": "",
        "live_reason": "",
        "is_need_double_stream": "false",
    }

    async with client:
        while not data.stop_event.is_set():
            try:
                async with client.stream(
                    "GET", f"https://live.douyin.com/{data.user_id}"
                ) as bootstrap_response:
                    bootstrap_response.raise_for_status()
            except httpx.HTTPError as error:
                log(f"Cookie bootstrap failed: {error}")
                await wait(data, 5.0)
                continue
            break

        while not data.stop_event.is_set():
            log("---")
            is_live = None
            is_normal = None
            stream_url = None
            try:
                response = await client.get(
                    "https://live.douyin.com/webcast/room/web/enter/",
                    params=params,
                )
                response.raise_for_status()
            except httpx.HTTPError as error:
                log(f"Live room request failed: {error}")
                await wait(data, 5.0)
                continue

            try:
                room = response.json()["data"]["data"][0]
                is_live = room["status"] == 2
                if is_live:
                    linker = room["linker_detail"]
                    is_normal = not any(
                        (
                            room["linker_map"],
                            linker["linker_play_modes"],
                            linker["function_type"],
                        )
                    )
                    if is_normal:
                        hls = room["stream_url"]["hls_pull_url_map"]
                        stream_url = (
                            hls.get("FULL_HD1")
                            or hls.get("HD1")
                            or hls.get("SD1")
                            or hls.get("SD2")
                            or next(iter(hls.values()), None)
                        )
            except (IndexError, KeyError, TypeError, ValueError) as error:
                log(f"Unsupported live room response: {error}")
                await wait(data, 5.0)
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

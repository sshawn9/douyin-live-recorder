from __future__ import annotations

import asyncio
import random

from .data import DouyinLiveData


async def wait(data: DouyinLiveData, timeout: float) -> None:
    timeout *= random.uniform(0.9, 1.1)
    try:
        await asyncio.wait_for(data.stop_event.wait(), timeout=timeout)
    except TimeoutError:
        pass

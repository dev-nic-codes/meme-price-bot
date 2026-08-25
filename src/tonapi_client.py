from __future__ import annotations

import asyncio
import os


class TonApiRequestPacer:
    """Serialize anonymous TonAPI request starts across bot services."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def wait(self) -> None:
        minimum_interval = 0.05 if os.getenv("TONAPI_KEY", "").strip() else 1.1
        loop = asyncio.get_running_loop()
        async with self._lock:
            delay = self._next_request_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = loop.time() + minimum_interval


TONAPI_REQUEST_PACER = TonApiRequestPacer()

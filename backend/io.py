"""Bound blocking SDK work to protect the 512 MiB production process."""

import asyncio
from typing import Any, Callable

limit = asyncio.Semaphore(8)


async def run_blocking(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    async with limit:
        # Cancellation must not free capacity while its worker still runs.
        task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

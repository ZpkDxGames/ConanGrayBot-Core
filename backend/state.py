"""Bounded expiry-aware state; active locks are never evicted."""

import time
from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLRegistry(MutableMapping[str, T], Generic[T]):
    def __init__(self, capacity: int = 1024, ttl: float = 3600):
        if capacity < 1 or ttl <= 0:
            raise ValueError("Invalid state limits")
        self.capacity, self.ttl = capacity, ttl
        self.rows: OrderedDict[str, tuple[float, T]] = OrderedDict()

    @staticmethod
    def busy(value: T) -> bool:
        return bool(
            getattr(value, "locked", lambda: False)()
            or getattr(value, "_waiters", None)
        )

    def prune(self) -> None:
        now = time.monotonic()
        for key, (stamp, value) in tuple(self.rows.items()):
            if now - stamp >= self.ttl and not self.busy(value):
                del self.rows[key]

    def __getitem__(self, key: str) -> T:
        self.prune()
        _, value = self.rows[key]
        self.rows[key] = (time.monotonic(), value)
        self.rows.move_to_end(key)
        return value

    def __setitem__(self, key: str, value: T) -> None:
        self.prune()
        if key not in self.rows and len(self.rows) >= self.capacity:
            for oldest, (_, candidate) in tuple(self.rows.items()):
                if not self.busy(candidate):
                    del self.rows[oldest]
                    break
            else:
                raise RuntimeError("All state entries are active")
        self.rows[key] = (time.monotonic(), value)
        self.rows.move_to_end(key)

    def __delitem__(self, key: str) -> None:
        del self.rows[key]

    def __len__(self) -> int:
        self.prune()
        return len(self.rows)

    def __iter__(self) -> Iterator[str]:
        self.prune()
        return iter(tuple(self.rows))

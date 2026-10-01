"""Bounded provider circuit and safe operational telemetry."""

import time
from dataclasses import dataclass


@dataclass
class ProviderState:
    failures: int = 0
    successes: int = 0
    blocked_until: float = 0
    last_latency_ms: int | None = None


class ProviderManager:
    def __init__(self):
        self.states = {
            name: ProviderState() for name in ("gemini", "groq", "openrouter")
        }

    def available(self, provider: str) -> bool:
        return time.monotonic() >= self.states[provider].blocked_until

    def failure(self, provider: str) -> None:
        state = self.states[provider]
        state.failures += 1
        if state.failures >= 3:
            state.blocked_until = time.monotonic() + 30

    def success(self, provider: str, started: float) -> None:
        state = self.states[provider]
        state.failures = 0
        state.blocked_until = 0
        state.successes += 1
        state.last_latency_ms = max(0, int((time.monotonic() - started) * 1000))

    def diagnostics(self) -> dict:
        return {
            name: {
                "available": self.available(name),
                "failures": row.failures,
                "successes": row.successes,
                "lastLatencyMs": row.last_latency_ms,
            }
            for name, row in self.states.items()
        }


manager = ProviderManager()

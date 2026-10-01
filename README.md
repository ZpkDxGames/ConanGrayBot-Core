# ConanGrayBot Core

Discord bot and versioned management API for the coordinated ConanGrayBot ecosystem. The `revamp/v2` branch is under reconstruction and is not certified for deployment or stable release. See WORK_PROGRESS.md for current evidence and remaining work.

## Development

Use Python 3.11 or 3.12. Keep secrets outside Git; `.env.example` lists configuration names only.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/ruff check backend tests
.venv/bin/ruff format --check backend tests
.venv/bin/mypy backend
.venv/bin/python main.py
```

Install runtime and development requirements separately because runtime dependencies enforce hashes. Deploy one worker on Discloud; production requires persistent Firebase storage and independent service/media secrets. Missing persistent credentials fail closed.

## API

Management operations use `/api/v1`. Every request requires `Authorization: Bearer CORE_SERVICE_TOKEN` and a body-bound actor proof; Core checks the configured guild and live Discord staff role. The browser accesses the Page BFF only. `/health/live` and `/health/ready` are minimal public probes.

Config responses contain schema 4 and a revision. PUT includes the full config and expected revision; stale writes return 409. Signed media links expire; old permanent URLs are invalid.

The existing AI persona, catalog, weather, games, commands, media, presence and lifecycle behavior is retained while its implementation is being modernized. Runtime integration, broader coverage and the remaining architectural work are not yet certified.

No license grant is assumed: the upstream repository did not include a LICENSE file.

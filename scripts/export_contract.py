"""Deterministic contract generation shared with the management Page."""

import json
from pathlib import Path

from backend.api import app
from backend.config import DEFAULT_BOT_CONFIG
from backend.migrations import migrate_config

for name, value in [
    ("openapi", app.openapi()),
    ("defaults", migrate_config(DEFAULT_BOT_CONFIG)),
]:
    Path("contracts", name + ".json").write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n"
    )

"""Pure, idempotent migration; preserves customized persona and catalog rows."""

import copy
from typing import Any

from .models import BotConfig


class RevisionConflict(ValueError):
    pass


def migrate_config(config: dict[str, Any]) -> dict[str, Any]:
    from .firebase_client import merge_bot_config

    original = copy.deepcopy(config)
    version = original.pop("schemaVersion", 1)
    revision = original.pop("revision", 0)
    if isinstance(version, bool) or version not in {1, 2, 3, 4}:
        raise ValueError("Unsupported configuration schema")
    merged = merge_bot_config(original) if version < 4 else original
    return BotConfig.model_validate(
        {**merged, "schemaVersion": 4, "revision": revision}
    ).model_dump()

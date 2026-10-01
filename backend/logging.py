"""Redact private credentials before any handler receives a formatted record."""

import json
import logging
import os
import re


def redact(value: str) -> str:
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED]",
        value,
        flags=re.S,
    )
    for name, secret in os.environ.items():
        if (
            len(secret) >= 8
            and re.search(r"TOKEN|SECRET|API_KEY|SIGNING|SESSION_KEY", name)
            and not name.endswith(("URI", "URL"))
        ):
            value = value.replace(secret, "[REDACTED]")
    return re.sub(
        r"(?i)([?&](?:sig|code|key|access_token|refresh_token)=)[^&\s]+",
        r"\1[REDACTED]",
        value,
    )


def safe_payload(value):
    if isinstance(value, dict):
        return {
            k: safe_payload(v)
            for k, v in value.items()
            if not re.search(r"(?i)secret|token|private.?key|authorization|password", k)
        }
    if isinstance(value, list):
        return [safe_payload(v) for v in value]
    return redact(value) if isinstance(value, str) else value


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "level": record.levelname,
                "logger": record.name,
                "message": redact(record.getMessage()),
                "exception": type(record.exc_info[1]).__name__
                if record.exc_info
                else None,
            }
        )


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

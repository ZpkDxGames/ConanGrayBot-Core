"""Bounded filtered record pages with stable Firestore keyset continuation."""

import copy
import re
from typing import Any

from .firebase_client import MemoryStore
from .io import run_blocking
from .retention import expired


async def record_page(
    store,
    guild_id: str,
    kind: str,
    *,
    limit: int = 50,
    cursor: str = "",
    search: str = "",
    media_type: str = "",
    channel_id: str = "",
) -> tuple[list[dict[str, Any]], str | None]:
    if kind not in {"logs", "media"}:
        raise ValueError("Invalid record kind")
    if (
        not 1 <= limit <= 100
        or len(cursor) > 128
        or len(search) > 100
        or (cursor and not re.fullmatch(r"[A-Za-z0-9_-]+", cursor))
    ):
        raise ValueError("Invalid pagination parameters")

    def matches(row):
        return (
            not expired(row)
            and (not media_type or row.get("mediaType") == media_type)
            and (not channel_id or str(row.get("channelId") or "") == channel_id)
        )

    if isinstance(store, MemoryStore):
        rows = [
            row
            for row in (store._logs if kind == "logs" else store._media).get(
                guild_id, []
            )
            if matches(row)
        ]
        if search:
            rows = [row for row in rows if search.lower() in str(row).lower()]
        offset = int(cursor or "0")
        page = copy.deepcopy(rows[offset : offset + limit])
        next_cursor = str(offset + limit) if offset + limit < len(rows) else None
    else:
        from google.cloud.firestore_v1.base_query import FieldFilter

        def work():
            collection = (
                store.client.collection("guilds")
                .document(guild_id)
                .collection("logs" if kind == "logs" else "media_archive")
            )
            query = collection.order_by("createdAt", direction="DESCENDING").order_by(
                "__name__", direction="DESCENDING"
            )
            if media_type:
                query = query.where(filter=FieldFilter("mediaType", "==", media_type))
            if channel_id:
                query = query.where(filter=FieldFilter("channelId", "==", channel_id))
            if cursor:
                snap = collection.document(cursor).get()
                if not snap.exists:
                    raise ValueError("Invalid continuation cursor")
                query = query.start_after(snap)
            docs = list(query.limit(limit + 1).stream())
            selected = docs[:limit]
            page = []
            for doc in selected:
                row = doc.to_dict() or {}
                row.setdefault("recordId", doc.id)
                if matches(row) and (not search or search.lower() in str(row).lower()):
                    page.append(row)
            return page, selected[-1].id if len(docs) > limit else None

        page, next_cursor = await run_blocking(work)
    for row in page:
        row.pop("expiresAt", None)
        if hasattr(row.get("createdAt"), "isoformat"):
            row["createdAt"] = row["createdAt"].isoformat()
    return page, next_cursor

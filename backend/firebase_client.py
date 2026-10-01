from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import (
    AI_CONAN_BEST_FRIEND_PERSONALITY,
    AI_CONAN_STRUCTURE_INSTRUCTIONS,
    AI_PERSONA_PROFILE_VERSION,
    AI_PREVIOUS_DEFAULT_PERSONALITY,
    AI_V2_BEST_FRIEND_PERSONALITY,
    AI_V2_STRUCTURE_INSTRUCTIONS,
    DEFAULT_BOT_CONFIG,
    GUESS_SONG_CATALOG_ROUNDS,
    GUESS_SONG_CATALOG_VERSION,
    GUESS_SONG_PREVIOUS_DEFAULT_ROWS,
    get_settings,
)

log = logging.getLogger("conan.firebase")


def _session_key(guild_id: str, channel_id: str, branch_id: str) -> str:
    return f"{guild_id}:{channel_id}:{branch_id}"


def _session_doc_id(channel_id: str, branch_id: str) -> str:
    return f"{channel_id}--{branch_id}"


class MemoryStore:
    def __init__(self) -> None:
        self._guilds: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._branch_refs: dict[str, str] = {}
        self._active_branches: dict[str, str] = {}
        self._logs: dict[str, list[dict[str, Any]]] = {}
        self._media: dict[str, list[dict[str, Any]]] = {}
        self._guessing_games: dict[str, dict[str, Any]] = {}

    async def get_config(self, guild_id: str) -> dict[str, Any]:
        config = self._guilds.get(guild_id)
        if not config:
            config = copy.deepcopy(DEFAULT_BOT_CONFIG)
            config["ai"]["channelId"] = get_settings().ai_channel_id
            config["games"]["allowedCategoryId"] = get_settings().allowed_category_id
            self._guilds[guild_id] = config
        return copy.deepcopy(config)

    async def set_config(self, guild_id: str, config: dict[str, Any]) -> dict[str, Any]:
        merged = merge_bot_config(config)
        self._guilds[guild_id] = merged
        await self.add_log(guild_id, "config.updated", {"source": "dashboard"})
        return copy.deepcopy(merged)

    async def get_branch_session(self, guild_id: str, channel_id: str, branch_id: str) -> dict[str, Any]:
        row = self._sessions.get(_session_key(guild_id, channel_id, branch_id))
        if not row:
            return {
                "channelId": channel_id,
                "branchId": branch_id,
                "messages": [],
                "latestBotMessageId": "",
                "rootMessageId": "",
                "messageIds": [],
            }
        return copy.deepcopy(row)

    async def set_branch_session(
        self,
        guild_id: str,
        channel_id: str,
        branch_id: str,
        messages: list[dict[str, Any]],
        *,
        latest_bot_message_id: str = "",
        root_message_id: str = "",
        message_ids: list[str] | None = None,
        make_active: bool = True,
    ) -> None:
        key = _session_key(guild_id, channel_id, branch_id)
        previous = self._sessions.get(key) or {}
        # Keep every participating user/bot message in the retained history mapped
        # to the same branch. Prune mappings that fell out of the history window so
        # the reply index stays bounded instead of growing forever.
        ref_ids = {str(item) for item in (message_ids or []) if str(item)}
        resolved_latest = str(latest_bot_message_id or previous.get("latestBotMessageId") or "")
        resolved_root = str(root_message_id or previous.get("rootMessageId") or "")
        if resolved_latest:
            ref_ids.add(resolved_latest)
        if resolved_root:
            ref_ids.add(resolved_root)
        previous_ref_ids = {str(item) for item in (previous.get("messageIds") or []) if str(item)}
        for message_id in previous_ref_ids - ref_ids:
            ref_key = f"{guild_id}:{channel_id}:{message_id}"
            if self._branch_refs.get(ref_key) == branch_id:
                self._branch_refs.pop(ref_key, None)

        row = {
            "channelId": channel_id,
            "branchId": branch_id,
            "messages": copy.deepcopy(messages),
            "latestBotMessageId": resolved_latest,
            "rootMessageId": resolved_root,
            "messageIds": sorted(ref_ids),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        self._sessions[key] = row
        for message_id in ref_ids:
            self._branch_refs[f"{guild_id}:{channel_id}:{message_id}"] = branch_id
        if make_active:
            self._active_branches[f"{guild_id}:{channel_id}"] = branch_id

    async def resolve_reply_branch(self, guild_id: str, channel_id: str, message_id: str) -> str | None:
        return self._branch_refs.get(f"{guild_id}:{channel_id}:{message_id}")

    async def get_active_branch(self, guild_id: str, channel_id: str) -> str | None:
        return self._active_branches.get(f"{guild_id}:{channel_id}")

    async def set_active_branch(self, guild_id: str, channel_id: str, branch_id: str) -> None:
        self._active_branches[f"{guild_id}:{channel_id}"] = branch_id

    # Compatibility helpers for older code/tests. The default branch is intentionally
    # not used by the Discord handler once branch-memory mode is enabled.
    async def get_session(self, guild_id: str, channel_id: str, branch_id: str = "default") -> list[dict[str, Any]]:
        row = await self.get_branch_session(guild_id, channel_id, branch_id)
        return copy.deepcopy(row.get("messages") or [])

    async def set_session(
        self,
        guild_id: str,
        channel_id: str,
        messages: list[dict[str, Any]],
        branch_id: str = "default",
    ) -> None:
        await self.set_branch_session(guild_id, channel_id, branch_id, messages)

    async def clear_session(self, guild_id: str, channel_id: str) -> None:
        prefix = f"{guild_id}:{channel_id}:"
        keys = [key for key in self._sessions if key.startswith(prefix)]
        for key in keys:
            row = self._sessions.pop(key, {})
            latest = str(row.get("latestBotMessageId") or "")
            if latest:
                self._branch_refs.pop(f"{guild_id}:{channel_id}:{latest}", None)
        ref_prefix = f"{guild_id}:{channel_id}:"
        for key in [key for key in self._branch_refs if key.startswith(ref_prefix)]:
            self._branch_refs.pop(key, None)
        self._active_branches.pop(f"{guild_id}:{channel_id}", None)

    async def clear_guild_sessions(self, guild_id: str) -> int:
        prefix = f"{guild_id}:"
        channel_ids = {
            str(row.get("channelId") or "")
            for key, row in self._sessions.items()
            if key.startswith(prefix) and row.get("channelId")
        }
        for key in [key for key in self._sessions if key.startswith(prefix)]:
            self._sessions.pop(key, None)
        for key in [key for key in self._branch_refs if key.startswith(prefix)]:
            self._branch_refs.pop(key, None)
        for key in [key for key in self._active_branches if key.startswith(prefix)]:
            self._active_branches.pop(key, None)
        return len(channel_ids)

    async def session_stats(self, guild_id: str) -> dict[str, int]:
        prefix = f"{guild_id}:"
        rows = [row for key, row in self._sessions.items() if key.startswith(prefix) and row.get("messages")]
        channels = {str(row.get("channelId") or "") for row in rows if row.get("channelId")}
        return {"channels": len(channels), "messages": sum(len(row.get("messages") or []) for row in rows)}

    async def set_guessing_game(
        self,
        guild_id: str,
        channel_id: str,
        bot_message_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        row = copy.deepcopy(state)
        row.update({
            "guildId": str(guild_id),
            "channelId": str(channel_id),
            "botMessageId": str(bot_message_id),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        })
        self._guessing_games[f"{guild_id}:{channel_id}:{bot_message_id}"] = row
        return copy.deepcopy(row)

    async def get_guessing_game(
        self,
        guild_id: str,
        channel_id: str,
        bot_message_id: str,
    ) -> dict[str, Any] | None:
        key = f"{guild_id}:{channel_id}:{bot_message_id}"
        row = self._guessing_games.get(key)
        if not row:
            return None
        expires_at = str(row.get("expiresAt") or "")
        if expires_at:
            try:
                expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if expires <= datetime.now(timezone.utc):
                    self._guessing_games.pop(key, None)
                    return None
            except ValueError:
                pass
        return copy.deepcopy(row)

    async def delete_guessing_game(
        self,
        guild_id: str,
        channel_id: str,
        bot_message_id: str,
    ) -> dict[str, Any] | None:
        row = self._guessing_games.pop(f"{guild_id}:{channel_id}:{bot_message_id}", None)
        return copy.deepcopy(row) if row else None

    async def add_log(self, guild_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
        row = {
            "event": event,
            "payload": payload or {},
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        self._logs.setdefault(guild_id, []).insert(0, row)
        self._logs[guild_id] = self._logs[guild_id][:150]

    async def list_logs(self, guild_id: str, limit: int = 80) -> list[dict[str, Any]]:
        return copy.deepcopy(self._logs.get(guild_id, [])[:limit])

    async def add_media_record(self, guild_id: str, record: dict[str, Any]) -> dict[str, Any]:
        row = copy.deepcopy(record)
        row.setdefault("recordId", uuid4().hex)
        row.setdefault("createdAt", datetime.now(timezone.utc).isoformat())
        self._media.setdefault(guild_id, []).insert(0, row)
        self._media[guild_id] = self._media[guild_id][:1000]
        return copy.deepcopy(row)

    async def list_media_records(
        self,
        guild_id: str,
        *,
        limit: int = 100,
        media_type: str = "",
        channel_id: str = "",
    ) -> list[dict[str, Any]]:
        rows = self._media.get(guild_id, [])
        if media_type:
            rows = [row for row in rows if str(row.get("mediaType") or "") == media_type]
        if channel_id:
            rows = [row for row in rows if str(row.get("channelId") or "") == channel_id]
        return copy.deepcopy(rows[: max(1, min(limit, 250))])

    async def get_media_record(self, guild_id: str, record_id: str) -> dict[str, Any] | None:
        for row in self._media.get(guild_id, []):
            if str(row.get("recordId") or "") == record_id:
                return copy.deepcopy(row)
        return None

    async def delete_media_record(self, guild_id: str, record_id: str) -> dict[str, Any] | None:
        rows = self._media.get(guild_id, [])
        for index, row in enumerate(rows):
            if str(row.get("recordId") or "") == record_id:
                removed = rows.pop(index)
                return copy.deepcopy(removed)
        return None

    async def media_stats(self, guild_id: str) -> dict[str, int]:
        rows = self._media.get(guild_id, [])
        return {
            "files": len(rows),
            "images": sum(1 for row in rows if row.get("mediaType") == "image"),
            "videos": sum(1 for row in rows if row.get("mediaType") == "video"),
            "bytes": sum(int(row.get("size") or 0) for row in rows),
        }


class FirestoreStore:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def get_config(self, guild_id: str) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            ref = self.client.collection("guilds").document(guild_id)
            snap = ref.get()
            if not snap.exists:
                config = copy.deepcopy(DEFAULT_BOT_CONFIG)
                config["ai"]["channelId"] = get_settings().ai_channel_id
                config["games"]["allowedCategoryId"] = get_settings().allowed_category_id
                ref.set({"config": config, "updatedAt": _server_timestamp()}, merge=True)
                return config
            data = snap.to_dict() or {}
            return merge_bot_config(data.get("config") or {})

        return await asyncio.to_thread(work)

    async def set_config(self, guild_id: str, config: dict[str, Any]) -> dict[str, Any]:
        merged = merge_bot_config(config)

        def work() -> None:
            self.client.collection("guilds").document(guild_id).set(
                {"config": merged, "updatedAt": _server_timestamp()},
                merge=True,
            )

        await asyncio.to_thread(work)
        await self.add_log(guild_id, "config.updated", {"source": "dashboard"})
        return merged

    def _sessions_collection(self, guild_id: str) -> Any:
        return self.client.collection("guilds").document(guild_id).collection("ai_sessions")

    def _refs_collection(self, guild_id: str) -> Any:
        return self.client.collection("guilds").document(guild_id).collection("ai_branch_refs")

    def _channel_state_collection(self, guild_id: str) -> Any:
        return self.client.collection("guilds").document(guild_id).collection("ai_channel_state")

    async def get_branch_session(self, guild_id: str, channel_id: str, branch_id: str) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            snap = self._sessions_collection(guild_id).document(_session_doc_id(channel_id, branch_id)).get()
            if not snap.exists:
                return {
                    "channelId": channel_id,
                    "branchId": branch_id,
                    "messages": [],
                    "latestBotMessageId": "",
                    "rootMessageId": "",
                    "messageIds": [],
                }
            data = snap.to_dict() or {}
            return {
                "channelId": str(data.get("channelId") or channel_id),
                "branchId": str(data.get("branchId") or branch_id),
                "messages": data.get("messages") or [],
                "latestBotMessageId": str(data.get("latestBotMessageId") or ""),
                "rootMessageId": str(data.get("rootMessageId") or ""),
                "messageIds": [str(item) for item in (data.get("messageIds") or []) if str(item)],
            }

        return await asyncio.to_thread(work)

    async def set_branch_session(
        self,
        guild_id: str,
        channel_id: str,
        branch_id: str,
        messages: list[dict[str, Any]],
        *,
        latest_bot_message_id: str = "",
        root_message_id: str = "",
        message_ids: list[str] | None = None,
        make_active: bool = True,
    ) -> None:
        def work() -> None:
            session_ref = self._sessions_collection(guild_id).document(_session_doc_id(channel_id, branch_id))
            previous_snap = session_ref.get()
            previous = previous_snap.to_dict() if previous_snap.exists else {}
            previous = previous or {}
            resolved_root = str(root_message_id or previous.get("rootMessageId") or "")
            resolved_latest = str(latest_bot_message_id or previous.get("latestBotMessageId") or "")

            ref_ids = {str(item) for item in (message_ids or []) if str(item)}
            if resolved_latest:
                ref_ids.add(resolved_latest)
            if resolved_root:
                ref_ids.add(resolved_root)
            previous_ref_ids = {str(item) for item in (previous.get("messageIds") or []) if str(item)}

            batch = self.client.batch()
            for message_id in previous_ref_ids - ref_ids:
                batch.delete(self._refs_collection(guild_id).document(message_id))
            batch.set(
                session_ref,
                {
                    "channelId": channel_id,
                    "branchId": branch_id,
                    "messages": messages,
                    "latestBotMessageId": resolved_latest,
                    "rootMessageId": resolved_root,
                    "messageIds": sorted(ref_ids),
                    "updatedAt": _server_timestamp(),
                },
                merge=False,
            )

            for message_id in ref_ids:
                batch.set(
                    self._refs_collection(guild_id).document(message_id),
                    {
                        "channelId": channel_id,
                        "branchId": branch_id,
                        "updatedAt": _server_timestamp(),
                    },
                    merge=False,
                )
            if make_active:
                batch.set(
                    self._channel_state_collection(guild_id).document(str(channel_id)),
                    {
                        "channelId": channel_id,
                        "activeBranchId": branch_id,
                        "updatedAt": _server_timestamp(),
                    },
                    merge=False,
                )
            batch.commit()

        await asyncio.to_thread(work)

    async def resolve_reply_branch(self, guild_id: str, channel_id: str, message_id: str) -> str | None:
        def work() -> str | None:
            snap = self._refs_collection(guild_id).document(str(message_id)).get()
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            if str(data.get("channelId") or "") != str(channel_id):
                return None
            branch_id = str(data.get("branchId") or "")
            return branch_id or None

        return await asyncio.to_thread(work)

    async def get_active_branch(self, guild_id: str, channel_id: str) -> str | None:
        def work() -> str | None:
            snap = self._channel_state_collection(guild_id).document(str(channel_id)).get()
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            branch_id = str(data.get("activeBranchId") or "")
            return branch_id or None

        return await asyncio.to_thread(work)

    async def set_active_branch(self, guild_id: str, channel_id: str, branch_id: str) -> None:
        def work() -> None:
            self._channel_state_collection(guild_id).document(str(channel_id)).set(
                {
                    "channelId": channel_id,
                    "activeBranchId": branch_id,
                    "updatedAt": _server_timestamp(),
                },
                merge=False,
            )

        await asyncio.to_thread(work)

    async def get_session(self, guild_id: str, channel_id: str, branch_id: str = "default") -> list[dict[str, Any]]:
        row = await self.get_branch_session(guild_id, channel_id, branch_id)
        return row.get("messages") or []

    async def set_session(
        self,
        guild_id: str,
        channel_id: str,
        messages: list[dict[str, Any]],
        branch_id: str = "default",
    ) -> None:
        await self.set_branch_session(guild_id, channel_id, branch_id, messages)

    async def clear_session(self, guild_id: str, channel_id: str) -> None:
        def work() -> None:
            sessions = list(self._sessions_collection(guild_id).where("channelId", "==", channel_id).stream())
            legacy = self._sessions_collection(guild_id).document(channel_id).get()
            refs = list(self._refs_collection(guild_id).where("channelId", "==", channel_id).stream())
            state = self._channel_state_collection(guild_id).document(str(channel_id)).get()
            raw_refs = [doc.reference for doc in sessions] + [doc.reference for doc in refs]
            if legacy.exists:
                raw_refs.append(legacy.reference)
            if state.exists:
                raw_refs.append(state.reference)
            doc_refs = list({ref.path: ref for ref in raw_refs}.values())
            for start in range(0, len(doc_refs), 400):
                batch = self.client.batch()
                for ref in doc_refs[start : start + 400]:
                    batch.delete(ref)
                batch.commit()

        await asyncio.to_thread(work)

    async def clear_guild_sessions(self, guild_id: str) -> int:
        def work() -> int:
            session_docs = list(self._sessions_collection(guild_id).stream())
            ref_docs = list(self._refs_collection(guild_id).stream())
            state_docs = list(self._channel_state_collection(guild_id).stream())
            channels: set[str] = set()
            for doc in session_docs:
                data = doc.to_dict() or {}
                channel_id = str(data.get("channelId") or "")
                if not channel_id and "--" not in doc.id:
                    channel_id = doc.id
                if channel_id:
                    channels.add(channel_id)
            all_refs = [doc.reference for doc in session_docs] + [doc.reference for doc in ref_docs] + [doc.reference for doc in state_docs]
            for start in range(0, len(all_refs), 400):
                batch = self.client.batch()
                for ref in all_refs[start : start + 400]:
                    batch.delete(ref)
                batch.commit()
            return len(channels)

        return await asyncio.to_thread(work)

    async def session_stats(self, guild_id: str) -> dict[str, int]:
        def work() -> dict[str, int]:
            channels: set[str] = set()
            messages = 0
            for doc in self._sessions_collection(guild_id).stream():
                data = doc.to_dict() or {}
                stored = data.get("messages") or []
                if not stored:
                    continue
                channel_id = str(data.get("channelId") or "")
                if not channel_id and "--" not in doc.id:
                    channel_id = doc.id
                if channel_id:
                    channels.add(channel_id)
                messages += len(stored)
            return {"channels": len(channels), "messages": messages}

        return await asyncio.to_thread(work)

    def _guessing_games_collection(self, guild_id: str) -> Any:
        return self.client.collection("guilds").document(guild_id).collection("guessing_games")

    async def set_guessing_game(
        self,
        guild_id: str,
        channel_id: str,
        bot_message_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        row = copy.deepcopy(state)
        row.update({
            "guildId": str(guild_id),
            "channelId": str(channel_id),
            "botMessageId": str(bot_message_id),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        })

        def work() -> None:
            self._guessing_games_collection(guild_id).document(str(bot_message_id)).set(row, merge=False)

        await asyncio.to_thread(work)
        return row

    async def get_guessing_game(
        self,
        guild_id: str,
        channel_id: str,
        bot_message_id: str,
    ) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            ref = self._guessing_games_collection(guild_id).document(str(bot_message_id))
            snap = ref.get()
            if not snap.exists:
                return None
            row = snap.to_dict() or {}
            if str(row.get("channelId") or "") != str(channel_id):
                return None
            expires_at = str(row.get("expiresAt") or "")
            if expires_at:
                try:
                    expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    if expires <= datetime.now(timezone.utc):
                        ref.delete()
                        return None
                except ValueError:
                    pass
            return row

        return await asyncio.to_thread(work)

    async def delete_guessing_game(
        self,
        guild_id: str,
        channel_id: str,
        bot_message_id: str,
    ) -> dict[str, Any] | None:
        row = await self.get_guessing_game(guild_id, channel_id, bot_message_id)
        if row is None:
            return None
        await asyncio.to_thread(self._guessing_games_collection(guild_id).document(str(bot_message_id)).delete)
        return row

    async def add_log(self, guild_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
        def work() -> None:
            (
                self.client.collection("guilds")
                .document(guild_id)
                .collection("logs")
                .add({"event": event, "payload": payload or {}, "createdAt": _server_timestamp()})
            )

        await asyncio.to_thread(work)

    async def list_logs(self, guild_id: str, limit: int = 80) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            query = (
                self.client.collection("guilds")
                .document(guild_id)
                .collection("logs")
                .order_by("createdAt", direction="DESCENDING")
                .limit(limit)
            )
            rows = []
            for doc in query.stream():
                data = doc.to_dict() or {}
                created = data.get("createdAt")
                if hasattr(created, "isoformat"):
                    data["createdAt"] = created.isoformat()
                rows.append(data)
            return rows

        return await asyncio.to_thread(work)

    def _media_collection(self, guild_id: str) -> Any:
        return self.client.collection("guilds").document(guild_id).collection("media_archive")

    async def add_media_record(self, guild_id: str, record: dict[str, Any]) -> dict[str, Any]:
        row = copy.deepcopy(record)
        record_id = str(row.get("recordId") or uuid4().hex)
        row["recordId"] = record_id

        def work() -> None:
            stored = copy.deepcopy(row)
            stored["createdAt"] = _server_timestamp()
            self._media_collection(guild_id).document(record_id).set(stored, merge=False)

        await asyncio.to_thread(work)
        row.setdefault("createdAt", datetime.now(timezone.utc).isoformat())
        return row

    async def list_media_records(
        self,
        guild_id: str,
        *,
        limit: int = 100,
        media_type: str = "",
        channel_id: str = "",
    ) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            # Filter in Python so installations do not need a custom Firestore
            # composite index for type/channel + createdAt combinations.
            query = self._media_collection(guild_id).order_by("createdAt", direction="DESCENDING").limit(250)
            rows: list[dict[str, Any]] = []
            for doc in query.stream():
                data = doc.to_dict() or {}
                if media_type and str(data.get("mediaType") or "") != media_type:
                    continue
                if channel_id and str(data.get("channelId") or "") != channel_id:
                    continue
                data.setdefault("recordId", doc.id)
                created = data.get("createdAt")
                if hasattr(created, "isoformat"):
                    data["createdAt"] = created.isoformat()
                rows.append(data)
                if len(rows) >= max(1, min(limit, 250)):
                    break
            return rows

        return await asyncio.to_thread(work)

    async def get_media_record(self, guild_id: str, record_id: str) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            snap = self._media_collection(guild_id).document(record_id).get()
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            data.setdefault("recordId", snap.id)
            created = data.get("createdAt")
            if hasattr(created, "isoformat"):
                data["createdAt"] = created.isoformat()
            return data

        return await asyncio.to_thread(work)

    async def delete_media_record(self, guild_id: str, record_id: str) -> dict[str, Any] | None:
        record = await self.get_media_record(guild_id, record_id)
        if record is None:
            return None
        await asyncio.to_thread(self._media_collection(guild_id).document(record_id).delete)
        return record

    async def media_stats(self, guild_id: str) -> dict[str, int]:
        def work() -> dict[str, int]:
            files = images = videos = total_bytes = 0
            for doc in self._media_collection(guild_id).stream():
                data = doc.to_dict() or {}
                files += 1
                images += int(data.get("mediaType") == "image")
                videos += int(data.get("mediaType") == "video")
                total_bytes += int(data.get("size") or 0)
            return {"files": files, "images": images, "videos": videos, "bytes": total_bytes}

        return await asyncio.to_thread(work)


def merge_bot_config(update: dict[str, Any] | None) -> dict[str, Any]:
    update = copy.deepcopy(update or {})
    merged = deep_merge(copy.deepcopy(DEFAULT_BOT_CONFIG), update)
    incoming_ai = update.get("ai") if isinstance(update.get("ai"), dict) else {}
    if "replyStyle" not in incoming_ai:
        merged["ai"]["replyStyle"] = "embed" if merged["ai"].get("embedReplies", True) else "plain"

    # Persona profile v3 makes the voice less scripted and exposes granular
    # naturalness controls without overwriting an intentionally customized
    # personality. Installations carrying a shipped v1/v2 prompt migrate once;
    # explicit dashboard choices remain authoritative afterward.
    try:
        persona_profile_version = int(incoming_ai.get("personaProfileVersion") or 0)
    except (TypeError, ValueError):
        persona_profile_version = 0
    if persona_profile_version < AI_PERSONA_PROFILE_VERSION:
        current_personality = str(incoming_ai.get("personality") or "").strip()
        migrating_shipped_persona = not current_personality or current_personality in {
            AI_PREVIOUS_DEFAULT_PERSONALITY,
            AI_V2_BEST_FRIEND_PERSONALITY,
        }
        if migrating_shipped_persona:
            merged["ai"]["personality"] = AI_CONAN_BEST_FRIEND_PERSONALITY

        current_structure = str(incoming_ai.get("structureInstructions") or "").strip()
        if not current_structure or (migrating_shipped_persona and current_structure == AI_V2_STRUCTURE_INSTRUCTIONS):
            merged["ai"]["structureInstructions"] = AI_CONAN_STRUCTURE_INSTRUCTIONS

        if migrating_shipped_persona:
            shipped_style_defaults = {
                "responseLength": ({"balanced", "brief"}, "brief"),
                "toneStyle": ({"adaptive", "casual", "natural"}, "natural"),
                "emojiStyle": ({"occasional", "rare"}, "rare"),
                "markdownStyle": ({"natural", "none"}, "none"),
                "temperature": ({0.8, 0.75, 0.85}, 0.85),
                "maxOutputTokens": ({650, 220, 260}, 260),
                "catchphraseCooldownTurns": ({8, 10}, 10),
            }
            for key, (old_values, new_value) in shipped_style_defaults.items():
                if key not in incoming_ai or incoming_ai.get(key) in old_values:
                    merged["ai"][key] = new_value

        natural_persona_defaults = {
            "strictPersonaStyle": True,
            "forceLowercase": True,
            "maxReplyCharacters": 420,
            "naturalnessLevel": 92,
            "mirroringLevel": 88,
            "questionFrequency": 18,
            "initiativeLevel": 22,
            "humorLevel": 62,
            "sarcasmLevel": 42,
            "emotionalOpenness": 68,
            "dramaticFlair": 38,
            "teasingLevel": 34,
            "slangLevel": 28,
            "lowEnergyStyle": "mirror",
            "comfortStyle": "soft_specific",
            "unknownStyle": "honest_funny",
            "affectionStyle": "subtle",
            "allowSentenceFragments": True,
            "avoidAssistantLanguage": True,
            "allowSelfDeprecation": True,
            "recurringBits": ["carrot cake", "i love gina!"],
            "avoidPhrases": [
                "silence says it all",
                "anything on your mind",
                "how can i help",
                "feel free to",
                "i'm here for you",
            ],
            "styleExamples": {
                "casual": "that's the whole movie night experience honestly. the choosing was the activity",
                "lowEnergy": "yeah. fair enough",
                "comfort": "aw i'm sorry :( that sounds genuinely awful",
                "unknown": "not even a little. google is about to become our third best friend lmao",
                "teasing": "that's not a purchase, that's a personality commitment",
            },
        }
        for key, default_value in natural_persona_defaults.items():
            if key not in incoming_ai:
                merged["ai"][key] = default_value
        if "personaPreset" not in incoming_ai:
            merged["ai"]["personaPreset"] = "public_conan" if migrating_shipped_persona else "custom"
        if "catchphraseCooldownTurns" not in incoming_ai:
            merged["ai"]["catchphraseCooldownTurns"] = 10

    merged["ai"]["personaProfileVersion"] = AI_PERSONA_PROFILE_VERSION

    # Message template profile v2 adds one independently configurable profile
    # per game. Existing installs used a single game profile that commonly
    # inherited Global defaults, which also forced narration/provider metadata
    # into the footer. Migrate only the shipped/default shape; custom game
    # profiles keep their existing inheritance and copy.
    incoming_presentation = update.get("presentation") if isinstance(update.get("presentation"), dict) else {}
    try:
        template_profile_version = int(incoming_presentation.get("messageTemplateProfileVersion") or 0)
    except (TypeError, ValueError):
        template_profile_version = 0
    if template_profile_version < 2:
        incoming_templates = update.get("messageTemplates") if isinstance(update.get("messageTemplates"), dict) else {}
        incoming_game = incoming_templates.get("game") if isinstance(incoming_templates.get("game"), dict) else {}
        shipped_game_fields = {
            "titleTemplate": "{title}",
            "descriptionTemplate": "{description}",
            "footerTemplate": "{footer}",
            "authorTemplate": "Requested by {actor}",
            "color": "",
            "thumbnailUrl": "",
            "useEmbed": True,
            "showRequester": True,
            "showTimestamp": True,
            "showFields": True,
        }
        game_is_shipped = (
            not incoming_game
            or (
                "showProvider" not in incoming_game
                and "showSourceNote" not in incoming_game
                and all(
                    key not in incoming_game or incoming_game.get(key) == value
                    for key, value in shipped_game_fields.items()
                )
            )
        )
        if game_is_shipped:
            merged["messageTemplates"]["game"]["inheritGlobal"] = False
            merged["messageTemplates"]["game"]["showProvider"] = False
            merged["messageTemplates"]["game"]["showSourceNote"] = False
        for game_template_key in (
            "game_tictactoe",
            "game_coinflip",
            "game_eightball",
            "game_rps",
            "game_guesssong",
            "game_wouldyourather",
        ):
            if game_template_key not in incoming_templates:
                merged["messageTemplates"][game_template_key]["inheritGlobal"] = True
        merged["presentation"]["messageTemplateProfileVersion"] = 2

    incoming_presence = update.get("presence") if isinstance(update.get("presence"), dict) else {}
    presence = merged.get("presence") if isinstance(merged.get("presence"), dict) else {}
    allowed_statuses = {"online", "idle", "dnd", "invisible"}
    allowed_activity_types = {"playing", "streaming", "listening", "watching", "competing"}
    raw_entries = incoming_presence.get("entries") if "entries" in incoming_presence else None
    if raw_entries is None and incoming_presence:
        raw_entries = [{
            "enabled": True,
            "status": presence.get("status", "online"),
            "activityType": presence.get("activityType", "listening"),
            "activityText": presence.get("activityText", "dramatic bridge sections"),
            "streamUrl": presence.get("streamUrl", ""),
        }]
    if raw_entries is None:
        raw_entries = presence.get("entries") or []

    normalized_entries: list[dict[str, Any]] = []
    if isinstance(raw_entries, list):
        for raw_entry in raw_entries[:20]:
            if not isinstance(raw_entry, dict):
                continue
            status = str(raw_entry.get("status") or "online").lower()
            activity_type = str(raw_entry.get("activityType") or "listening").lower()
            normalized_entries.append({
                "enabled": bool(raw_entry.get("enabled", True)),
                "status": status if status in allowed_statuses else "online",
                "activityType": activity_type if activity_type in allowed_activity_types else "listening",
                "activityText": str(raw_entry.get("activityText") or "").strip()[:128],
                "streamUrl": str(raw_entry.get("streamUrl") or "").strip()[:500],
            })
    if not normalized_entries:
        normalized_entries = [{
            "enabled": True,
            "status": "online",
            "activityType": "listening",
            "activityText": "dramatic bridge sections",
            "streamUrl": "",
        }]
    presence["entries"] = normalized_entries
    presence["rotationEnabled"] = bool(presence.get("rotationEnabled", False))
    try:
        presence["intervalSeconds"] = max(15, min(86400, int(presence.get("intervalSeconds") or 60)))
    except (TypeError, ValueError):
        presence["intervalSeconds"] = 60
    first_enabled = next((entry for entry in normalized_entries if entry.get("enabled")), normalized_entries[0])
    presence["status"] = first_enabled["status"]
    presence["activityType"] = first_enabled["activityType"]
    presence["activityText"] = first_enabled["activityText"]
    presence["streamUrl"] = first_enabled["streamUrl"]
    merged["presence"] = presence

    incoming_media = update.get("media") if isinstance(update.get("media"), dict) else {}
    media = merged.get("media") if isinstance(merged.get("media"), dict) else {}
    # Version 2 restores the original Discord delivery layout for videos:
    # a native attachment/player followed by the configured feedback embed.
    # Configurations saved by the temporary Components V2 release did not carry
    # a version marker, so migrate them once while still allowing an explicit
    # inline-card selection to persist after the updated dashboard saves v2.
    try:
        media_mode_version = int(incoming_media.get("videoDisplayModeVersion") or 0)
    except (TypeError, ValueError):
        media_mode_version = 0
    if media_mode_version < 2:
        media["videoDisplayMode"] = "embed_attachment"
    if str(media.get("videoDisplayMode") or "") not in {"embed_attachment", "inline_card"}:
        media["videoDisplayMode"] = "embed_attachment"
    media["videoDisplayModeVersion"] = 2
    merged["media"] = media

    incoming_games = update.get("games") if isinstance(update.get("games"), dict) else {}
    if "guessSongRounds" not in incoming_games and "guessSongHints" in incoming_games:
        legacy_hints = [str(item).strip() for item in incoming_games.get("guessSongHints") or [] if str(item).strip()]
        legacy_answers = ["Heather", "Maniac", "People Watching"]
        migrated = [
            f"{legacy_answers[index]} | | {hint}"
            for index, hint in enumerate(legacy_hints[: len(legacy_answers)])
        ]
        if migrated:
            merged["games"]["guessSongRounds"] = migrated

    # Add the expanded lyric-clue catalog once for existing guilds. Missing song
    # titles are appended, while only untouched rows from the previous defaults
    # are replaced. Custom dashboard edits are preserved.
    try:
        catalog_version = int(incoming_games.get("guessSongCatalogVersion") or 0)
    except (TypeError, ValueError):
        catalog_version = 0
    if catalog_version < GUESS_SONG_CATALOG_VERSION:
        rounds = list(merged.get("games", {}).get("guessSongRounds") or [])

        def title_key(raw_round: Any) -> str:
            if isinstance(raw_round, dict):
                title = str(raw_round.get("answer") or "")
            else:
                title = str(raw_round).split("|", 1)[0]
            return re.sub(r"[^a-z0-9]+", "", title.casefold())

        round_indexes: dict[str, int] = {}
        for index, raw_round in enumerate(rounds):
            key = title_key(raw_round)
            if key and key not in round_indexes:
                round_indexes[key] = index

        previous_defaults = {
            title_key(title): {str(row).strip() for row in old_rows}
            for title, old_rows in GUESS_SONG_PREVIOUS_DEFAULT_ROWS.items()
        }
        for catalog_row in GUESS_SONG_CATALOG_ROUNDS:
            key = title_key(catalog_row)
            existing_index = round_indexes.get(key)
            if existing_index is None:
                round_indexes[key] = len(rounds)
                rounds.append(catalog_row)
                continue
            existing_row = rounds[existing_index]
            if isinstance(existing_row, str) and existing_row.strip() in previous_defaults.get(key, set()):
                rounds[existing_index] = catalog_row

        merged["games"]["guessSongRounds"] = rounds
        merged["games"]["guessSongCatalogVersion"] = GUESS_SONG_CATALOG_VERSION
    return merged


def deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _server_timestamp() -> Any:
    try:
        from firebase_admin import firestore

        return firestore.SERVER_TIMESTAMP
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _load_service_account() -> dict[str, Any] | None:
    settings = get_settings()
    raw = settings.firebase_service_account_json
    if raw:
        try:
            if raw.strip().startswith("{"):
                return json.loads(raw)
            return json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception:
            log.exception("Could not parse FIREBASE_SERVICE_ACCOUNT_JSON")
            return None

    path = settings.firebase_service_account_path
    if path and Path(path).exists():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            log.exception("Could not read Firebase service account path")
            return None

    google_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if google_credentials and Path(google_credentials).exists():
        try:
            return json.loads(Path(google_credentials).read_text(encoding="utf-8"))
        except Exception:
            log.exception("Could not read GOOGLE_APPLICATION_CREDENTIALS")
            return None

    return None


def create_store() -> MemoryStore | FirestoreStore:
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        service_account = _load_service_account()
        if not service_account:
            log.warning("Firebase credentials not configured; using in-memory store.")
            return MemoryStore()

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(service_account))

        log.info("Firebase initialized successfully.")
        return FirestoreStore(firestore.client())
    except Exception:
        log.exception("Firebase initialization failed; using in-memory store.")
        return MemoryStore()

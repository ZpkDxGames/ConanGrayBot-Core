"""Shared response and mutation contract exported into the Page type generator."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldError(ResponseModel):
    path: str
    message: str


class ErrorEnvelope(ResponseModel):
    code: str
    message: str
    requestId: str
    fields: list[FieldError] = Field(default_factory=list)


class Live(ResponseModel):
    ok: bool
    version: str


class Ready(ResponseModel):
    ok: bool


class AuthCheck(ResponseModel):
    allowed: bool
    actorId: str
    guildId: str


class Compatibility(ResponseModel):
    coreVersion: str
    apiVersion: str
    schemaVersion: int


class ProviderMetrics(ResponseModel):
    available: bool
    failures: int
    successes: int
    lastLatencyMs: int | None


class Diagnostics(ResponseModel):
    version: str
    botReady: bool
    commandSync: str
    store: str
    providers: dict[str, bool]
    providerMetrics: dict[str, ProviderMetrics]
    weatherConfigured: bool
    driveConfigured: bool


class MemoryStats(ResponseModel):
    channels: int
    messages: int


class PresenceState(ResponseModel):
    active: bool
    guildId: str = ""
    currentIndex: int
    current: dict[str, JsonValue]


class AdminStatus(ResponseModel):
    guildId: str
    botStatus: str
    aiEnabled: bool
    adminRoleId: str
    memory: MemoryStats
    presenceRotation: PresenceState


class BotActionResult(ResponseModel):
    ok: bool
    action: Literal["start", "restart", "shutdown"]
    status: str
    user: str | None = None


class MemoryClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    allChannels: bool = False
    channelId: str = Field(default="", pattern=r"^\d*$", max_length=20)


class MemoryClearResult(ResponseModel):
    ok: bool
    scope: Literal["all", "channel"]
    clearedChannels: int | None = None
    channelId: str | None = None


class AIActionResult(ResponseModel):
    ok: bool
    aiEnabled: bool


class Channel(ResponseModel):
    id: str
    name: str
    mention: str
    categoryId: str
    type: str


class Category(ResponseModel):
    id: str
    name: str


class ChannelCatalog(ResponseModel):
    guildId: str
    channels: list[Channel]
    categories: list[Category]
    botReady: bool
    source: str | None = None
    error: str | None = None
    inviteUrl: str | None = None


class Command(ResponseModel):
    key: str
    name: str
    category: str
    description: str
    enabled: bool
    registered: bool


class CommandCatalog(ResponseModel):
    guildId: str
    commands: list[Command]
    syncStatus: str
    syncError: str | None
    manifestHash: str | None = None


class CommandSyncResult(ResponseModel):
    ok: bool
    registered: list[str]
    count: int


class DriveStatus(ResponseModel):
    configured: bool
    authMode: str
    identityLabel: str
    principalEmail: str
    serviceAccountEmail: str
    credentialProjectId: str
    expectedProjectId: str
    projectAligned: bool
    firebaseFallbackEnabled: bool
    oauthClientConfigured: bool
    oauthCredentialsComplete: bool
    oauthClientProjectId: str
    oauthClientActive: bool
    folderId: str


class DriveTestResult(DriveStatus):
    ok: bool
    folder: dict[str, JsonValue]


class RecordsPage(ResponseModel):
    guildId: str
    items: list[dict[str, JsonValue]]
    nextCursor: str | None
    stats: dict[str, int] | None = None
    drive: DriveStatus | None = None


class MediaDeleteResult(ResponseModel):
    ok: bool
    record: dict[str, JsonValue] | None
    driveFileDeleted: bool


class SandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=4000)


class SandboxResult(ResponseModel):
    answer: str
    provider: str
    latencyMs: int

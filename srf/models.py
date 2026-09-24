from __future__ import annotations
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field

class Isolation(str, Enum):
    PROCESS = "process"
    CONTAINER = "container"
    VM = "vm"
    QUBES = "qubes"
    REMOTE = "remote"

class Transport(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"
    SSE_LEGACY = "sse-legacy"

class PluginManifest(BaseModel):
    api_version: str = "srf/v1"
    id: str
    version: str
    name: str
    description: str = ""
    vendor: str | None = None
    transport: Transport
    endpoint: str | None = None
    command: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    isolation: Isolation = Isolation.PROCESS
    skills: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    source: str | None = None
    license: str | None = None
    digest: str | None = None
    signature: str | None = None
    trusted: bool = False

class Invocation(BaseModel):
    plugin_id: str
    operation: Literal["tool", "resource", "prompt", "task"]
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    approval_token: str | None = None

class InvocationResult(BaseModel):
    ok: bool
    plugin_id: str
    name: str
    result: Any = None
    error: str | None = None
    trace_id: str | None = None

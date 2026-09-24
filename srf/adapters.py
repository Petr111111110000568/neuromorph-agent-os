from typing import Any
from .models import PluginManifest

class Adapter:
    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError

class MCPAdapter(Adapter):
    """
    SDK boundary. Bind this to the official MCP SDK without leaking SDK
    details into SRF orchestration code.
    """
    def __init__(self, manifest: PluginManifest):
        self.manifest = manifest

    async def invoke(self, name, arguments):
        raise NotImplementedError(
            "Bind MCPAdapter to the installed official MCP SDK; "
            "the upstream plugin itself does not need to be rewritten."
        )

class LegacySidecarAdapter(Adapter):
    """
    Compatibility boundary for non-MCP applications. The application remains
    unchanged; a separate sidecar exposes MCP and translates to its native API.
    """
    def __init__(self, target: str):
        self.target = target

    async def invoke(self, name, arguments):
        raise NotImplementedError("Implement this in an external sidecar.")

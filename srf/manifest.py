from dataclasses import dataclass, field
from pathlib import Path
import json

@dataclass
class PluginManifest:
    id: str
    version: str
    protocol: str = "mcp"
    entrypoint: str | None = None
    capabilities: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    isolation: str = "process"
    permissions: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "PluginManifest":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))

    def validate(self) -> list[str]:
        errors = []
        if not self.id: errors.append("id is required")
        if not self.version: errors.append("version is required")
        if self.protocol not in {"mcp", "native", "sidecar"}:
            errors.append("unsupported protocol")
        if self.isolation not in {"process", "container", "microvm", "qube", "remote"}:
            errors.append("unsupported isolation mode")
        return errors

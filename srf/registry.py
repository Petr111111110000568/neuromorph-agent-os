from pathlib import Path
import json

class PluginManifest:
    def __init__(self, **data):
        self.id = data.get("id", "")
        self.name = data.get("name") or self.id
        self.version = data.get("version", "")
        self.transport = data.get("transport") or data.get("protocol", "mcp")
        self.protocol = data.get("protocol") or self.transport
        self.entrypoint = data.get("entrypoint")
        self.capabilities = data.get("capabilities", [])
        self.skills = data.get("skills", [])
        self.isolation = data.get("isolation", "process")
        self.permissions = data.get("permissions", [])
        self.metadata = data.get("metadata", {})

    @classmethod
    def model_validate_json(cls, raw):
        return cls(**json.loads(raw))

class PluginRegistry:
    def __init__(self, root):
        self.root = Path(root)

    def discover(self):
        return [
            PluginManifest.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self.root.glob("*/plugin.json"))
        ]

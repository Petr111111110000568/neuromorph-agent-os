from dataclasses import dataclass
from .manifest import PluginManifest

@dataclass
class Route:
    capability: str
    plugin_id: str
    score: float

class CapabilityRouter:
    def __init__(self):
        self.plugins: list[PluginManifest] = []

    def add(self, manifest: PluginManifest):
        self.plugins.append(manifest)

    def resolve(self, capability: str) -> list[Route]:
        routes = []
        for p in self.plugins:
            if capability in p.capabilities:
                score = 1.0
                if p.protocol == "mcp": score += 1.0
                if p.isolation in {"container", "microvm", "qube"}: score += 0.5
                routes.append(Route(capability, p.id, score))
        return sorted(routes, key=lambda r: r.score, reverse=True)

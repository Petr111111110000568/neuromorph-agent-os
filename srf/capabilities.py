from dataclasses import dataclass, field

@dataclass(frozen=True)
class Capability:
    name: str
    kind: str = "tool"
    description: str = ""
    risk: str = "low"
    tags: tuple[str, ...] = ()

@dataclass
class CapabilityCatalog:
    items: dict[str, Capability] = field(default_factory=dict)

    def register(self, capability: Capability) -> None:
        self.items[capability.name] = capability

    def match(self, required: list[str]) -> list[Capability]:
        return [c for name, c in self.items.items() if name in required]

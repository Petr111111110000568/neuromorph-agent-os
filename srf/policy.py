from dataclasses import dataclass

@dataclass
class PolicyDecision:
    allowed: bool
    reason: str

class PolicyEngine:
    def __init__(self, denied: set[str] | None = None):
        self.denied = denied or set()

    def check(self, capability: str, permissions: list[str]) -> PolicyDecision:
        if capability in self.denied:
            return PolicyDecision(False, f"capability denied: {capability}")
        return PolicyDecision(True, "allowed by default policy")

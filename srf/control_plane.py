import uuid
from .adapters import MCPAdapter
from .models import Invocation, InvocationResult
from .policy import PolicyEngine
from .registry import PluginRegistry

class ControlPlane:
    """SRF orchestration authority; MCP is its interoperability protocol."""
    def __init__(self, registry: PluginRegistry, policy=None):
        self.registry = registry
        self.policy = policy or PolicyEngine()

    async def invoke(self, request: Invocation):
        trace_id = request.trace_id or str(uuid.uuid4())
        try:
            plugin = self.registry.get(request.plugin_id)
            self.policy.authorize(plugin, request)
            result = await MCPAdapter(plugin).invoke(request.name, request.arguments)
            return InvocationResult(ok=True, plugin_id=plugin.id,
                                    name=request.name, result=result,
                                    trace_id=trace_id)
        except Exception as exc:
            return InvocationResult(ok=False, plugin_id=request.plugin_id,
                                    name=request.name, error=str(exc),
                                    trace_id=trace_id)

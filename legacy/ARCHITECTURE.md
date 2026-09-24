# SRF Plugin Fabric contract

## MCP
Use MCP as the common interoperability protocol for tools, resources, prompts,
and supported extensions such as Tasks. Keep transport details behind the MCP
adapter.

## Agent Skills
Use portable Agent Skills as the procedure/instruction layer:

```text
skill/
  SKILL.md
  scripts/
  references/
  assets/
```

Skills describe *how to work*. MCP describes *what capabilities are available*.

## SRF Control Plane
Own:
- discovery and registry
- trust and verification
- capability routing
- policy and authorization
- orchestration and DAGs
- retries/timeouts
- task lifecycle
- provenance and audit
- budgets
- isolation selection
- human approval gates

## No-rewrite rule
Preferred integration order:
1. Native MCP server
2. Remote MCP service
3. Official/maintained MCP wrapper
4. Isolated MCP sidecar around unchanged software
5. Native SRF adapter only as a last resort

## Isolation
Protocol and isolation are independent:

```text
MCP endpoint
  +-- process
  +-- container
  +-- VM
  +-- Qubes qube
  +-- remote trust domain
```

## Trust lifecycle

```text
discover -> inspect -> verify -> disposable test -> capability inventory
         -> policy assignment -> trusted -> production
```

Discovery never grants execution permission.

## Organic composition

```text
Quest
  +-- skill: research
  +-- MCP: literature
  +-- MCP: OSINT
  +-- MCP: bioinformatics
  +-- MCP: simulation
  +-- MCP: skeptic
  +-- MCP: replication
  +-- MCP: evidence
```

## Long-running research

Plugin task IDs are execution handles. SRF's event store remains the canonical
research record. This avoids coupling research state to any one plugin.

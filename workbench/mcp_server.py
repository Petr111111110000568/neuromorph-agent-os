"""Bounded MCP stdio subset pinned to protocol 2025-11-25.

Implements initialization, ping, tools/list and tools/call. It does not implement
remote HTTP transport, OAuth, resources, prompts, sampling or full conformance.
"""
import json
import sys
from . import __version__
from .service import ServiceError, object_field, parse_json

PROTOCOL = "2025-11-25"
SCHEMAS = {
  "list_sources": {"type": "object", "properties": {"q": {"type": "string"}}, "additionalProperties": False},
  "list_plugins": {"type": "object", "properties": {}, "additionalProperties": False},
  "run_plugin": {"type": "object", "properties": {"plugin_id": {"type": "string"}, "parameters": {"type": "object"}}, "required": ["plugin_id"], "additionalProperties": False},
  "review_question": {"type": "object", "properties": {"question": {"type": "string"}, "source_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["question"], "additionalProperties": False},
  "run_workflow": {"type": "object", "properties": {"question": {"type": "string"}, "plugin_id": {"type": "string"}, "parameters": {"type": "object"}, "source_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["question", "plugin_id"], "additionalProperties": False},
  "export_snapshot": {"type": "object", "properties": {}, "additionalProperties": False},
}
DESCRIPTIONS = {
  "list_sources": "Search locally registered research sources; does not browse or verify articles.",
  "list_plugins": "List pinned built-in numerical demonstrations and parameter schemas.",
  "run_plugin": "Run a bounded built-in numerical demonstration and persist provenance.",
  "review_question": "Apply eight deterministic advisory rules; no LLM or scientific verification.",
  "run_workflow": "Persist a rule-based review and a numerical demonstration as a workflow.",
  "export_snapshot": "Export current local sources, runs and integrity journal.",
}

SCHEMAS.update({
  "list_resources": {"type": "object", "properties": {"q": {"type": "string"}}, "additionalProperties": False},
  "plan_research": {"type": "object", "properties": {"question": {"type": "string", "maxLength": 2000}}, "required": ["question"], "additionalProperties": False},
  "start_campaign": {"type": "object", "properties": {"question": {"type": "string", "maxLength": 2000}, "max_iterations": {"type": "integer", "minimum": 1, "maximum": 5}, "max_jobs": {"type": "integer", "minimum": 1, "maximum": 20}, "online": {"type": "boolean"}, "simulate": {"type": "boolean"}}, "required": ["question"], "additionalProperties": False},
  "network_status": {"type": "object", "properties": {}, "additionalProperties": False},
  "advance_campaign": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
  "cancel_campaign": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
  "discover_resources": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 500}, "offline": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["query"], "additionalProperties": False},
})
DESCRIPTIONS.update({
  "list_resources": "Search local capability catalog and ingested scientific metadata; availability remains separate from discovery.",
  "plan_research": "Produce a rule-based evidence and model-validation plan; does not design biological interventions.",
  "start_campaign": "Persist a bounded research campaign; workers and coordinator execute discovery and synthetic controls. online=false by default.",
  "network_status": "Inspect workers, durable jobs and bounded campaigns without returning lease or auth tokens.",
  "advance_campaign": "Review completed jobs and enqueue the next bounded campaign iteration.",
  "cancel_campaign": "Cancel campaign and active jobs; stale worker results are rejected.",
  "discover_resources": "Queue a metadata search using fixed public providers or offline catalog; requires a connected worker.",
})


SCHEMAS.update({
    "search_agent_directory": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 500}, "provider": {"type": "string", "enum": ["agentverse", "huggingface_models", "huggingface_datasets"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 8}, "offline": {"type": "boolean"}, "data_class": {"type": "string", "enum": ["public", "internal", "sensitive_genomic"]}}, "required": ["query"], "additionalProperties": False},
    "society_status": {"type": "object", "properties": {}, "additionalProperties": False},
    "list_research_agents": {"type": "object", "properties": {"q": {"type": "string", "maxLength": 500}}, "additionalProperties": False},
    "route_research": {"type": "object", "properties": {"question": {"type": "string", "maxLength": 2000}, "data_class": {"type": "string", "enum": ["public", "internal", "sensitive_genomic"]}, "network_layer": {"type": "string", "enum": ["clear_web", "authenticated", "private", "onion"]}}, "required": ["question"], "additionalProperties": False},
    "start_research_mission": {"type": "object", "properties": {"question": {"type": "string", "maxLength": 2000}, "online": {"type": "boolean"}, "max_sources": {"type": "integer", "minimum": 1, "maximum": 8}, "data_class": {"type": "string", "enum": ["public", "internal", "sensitive_genomic"]}}, "required": ["question"], "additionalProperties": False},
    "advance_research_mission": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
    "cancel_research_mission": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
    "record_research_claim": {"type": "object", "properties": {"mission_id": {"type": "string"}, "text": {"type": "string"}, "scope": {"type": "string"}, "source_ids": {"type": "array", "items": {"type": "string"}}, "assessment": {"type": "string", "enum": ["unreviewed", "supported_within_scope", "contradicted", "inconclusive"]}, "rationale": {"type": "string"}}, "required": ["mission_id", "text", "scope"], "additionalProperties": False},
})
DESCRIPTIONS.update({
    "search_agent_directory": "Queue a bounded public Agentverse or Hugging Face metadata lookup; never contacts listed agents, downloads weights or grants access. Offline defaults to true.",
    "society_status": "Inspect research missions, documented external platforms and deterministic workflow roles; does not imply connected LLMs.",
    "list_research_agents": "Search documented scientific agents, platforms and protocols with organizations, licenses and evidence limits.",
    "route_research": "Produce lexical resource routing and data access policy; does not connect to gated resources or Tor.",
    "start_research_mission": "Queue three bounded jobs: metadata, abstract triage and synthetic method control; online=false by default. Not biological design.",
    "advance_research_mission": "Validate returned envelopes and provenance and update a persisted mission.",
    "cancel_research_mission": "Cancel a mission and reject late results via fenced queue leases.",
    "record_research_claim": "Record a user's scoped judgment with source references; it is not independent scientific validation.",
})


SCHEMAS.update({
    'federation_status': {'type':'object','properties':{},'additionalProperties':False},
    'discover_agent_candidates': {'type':'object','properties':{'query':{'type':'string','maxLength':500},'providers':{'type':'array','items':{'type':'string','enum':['agentverse','huggingface_models','huggingface_datasets','mcp_registry']}},'limit':{'type':'integer','minimum':1,'maximum':8},'online':{'type':'boolean'},'data_class':{'type':'string','enum':['public']}},'required':['query'],'additionalProperties':False},
    'draft_contribution_proposal': {'type':'object','properties':{'candidate_id':{'type':'string'},'offer_id':{'type':'string'}},'required':['candidate_id','offer_id'],'additionalProperties':False},
})
DESCRIPTIONS.update({
    'federation_status':'Inspect bounded discovery and voluntary contribution exchange. Does not imply connected external AI or verified science.',
    'discover_agent_candidates':'Read four public metadata directories or local catalog, persist candidates. Offline by default. No invitations, joins, downloads or code execution.',
    'draft_contribution_proposal':'Draft a task-for-access proposal for a discovered candidate and local offer. Does not send a message or grant membership.',
})


SCHEMAS.update({
    'brain_status': {'type':'object','properties':{},'additionalProperties':False},
    'start_brain_session': {'type':'object','properties':{
        'question':{'type':'string','minLength':1,'maxLength':2000},
        'plugins':{'type':'array','minItems':1,'maxItems':3,'uniqueItems':True,'items':{'type':'string','enum':['kan_benchmark','cortical_sequence','structural_plasticity']}},
        'seed':{'type':'integer','minimum':0,'maximum':2147483647},
        'data_class':{'type':'string','enum':['public','internal']}},'required':['question'],'additionalProperties':False},
    'get_brain_session': {'type':'object','properties':{'id':{'type':'string','minLength':1,'maxLength':100}},'required':['id'],'additionalProperties':False},
    'advance_brain_session': {'type':'object','properties':{'id':{'type':'string','minLength':1,'maxLength':100}},'required':['id'],'additionalProperties':False},
    'cancel_brain_session': {'type':'object','properties':{'id':{'type':'string','minLength':1,'maxLength':100}},'required':['id'],'additionalProperties':False},
})
DESCRIPTIONS.update({
    'brain_status':'Inspect deterministic specialist roles with anatomical analogies, bounded sessions and local worker status. No brain simulation or independent LLM minds.',
    'start_brain_session':'Plan local sources and episodic context, enqueue up to three synthetic numerical checks for trusted workers. No online search or biological intervention design.',
    'get_brain_session':'Read the shared workspace, role events, separate model results and provenance for one local session.',
    'advance_brain_session':'Validate completed trusted-worker result envelopes and integrate separate findings with explicit limits. Not scientific validation.',
    'cancel_brain_session':'Cancel a persisted specialist session and recover and cancel its queued jobs, including partial submissions.',
})


SCHEMAS.update({
    'harness_status': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    'harness_runs': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    'run_harness_task': {'type': 'object', 'properties': {
        'harness_id': {'type': 'string', 'enum': ['unreal', 'pi']},
        'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 12000},
        'provider': {'type': 'string', 'enum': ['openai']},
        'model': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 60}},
        'required': ['harness_id', 'prompt'], 'additionalProperties': False},
})
DESCRIPTIONS.update({
    'harness_status': 'Inspect installed external harness adapters and integrity pins. Does not call models.',
    'harness_runs': 'Read locally saved harness results and provenance. May contain supplied task text echoed by a model.',
    'run_harness_task': 'Send only the supplied public text to a configured LLM using Unreal or Pi, with OS tools disabled. Requires server AUTONOMY_ALLOW_MODEL_CALLS=true and credentials. May incur provider charges; timeout is not a token or cost budget. Returns an unverified model answer, not a scientific validation.',
})


def serve_stdio(service, reader=None, writer=None):
    # MCP uses UTF-8 independently of the host locale. Reconfigure only our
    # default streams; caller-provided text streams keep their own lifecycle.
    if reader is None:
        reader = sys.stdin
        reader.reconfigure(encoding="utf-8", errors="strict")
    if writer is None:
        writer = sys.stdout
        writer.reconfigure(encoding="utf-8", errors="strict", newline="\n")
    initialized = False
    ready = False
    while True:
        line = reader.readline(131074)
        if not line:
            return
        request_id = None
        notification = False
        try:
            if len(line.encode("utf-8")) > 131072:
                # Drain one oversized frame before accepting the next line.
                while line and not line.endswith("\n"):
                    line = reader.readline(131074)
                raise ServiceError("MCP frame exceeds 128 KiB", "parse_error")
            request = parse_json(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}
            else:
                notification = "id" not in request
                request_id = request.get("id")
                if isinstance(request_id, (dict, list, bool)):
                    response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request id"}}
                    notification = False
                    raise _Response(response)
                method = request["method"]
                params = object_field(request.get("params", {}), "params")
                if method == "initialize":
                    if initialized:
                        raise ServiceError("Already initialized", "invalid_request")
                    if not isinstance(params.get("protocolVersion"), str) or not isinstance(params.get("capabilities", {}), dict) or not isinstance(params.get("clientInfo", {}), dict):
                        raise ServiceError("Invalid initialize parameters")
                    initialized = True
                    result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "meta-harness-local", "version": __version__},
                              "instructions": "Local bounded research tools; rule-based advisors. MCP stdio subset 2025-11-25, not a full protocol implementation."}
                elif method == "notifications/initialized":
                    if initialized:
                        ready = True
                    continue
                elif method.startswith("notifications/"):
                    continue
                elif method == "ping":
                    result = {}
                elif not ready:
                    raise ServiceError("Initialize and send notifications/initialized first", "not_initialized")
                elif method == "tools/list":
                    result = {"tools": [{"name": name, "description": DESCRIPTIONS[name], "inputSchema": schema}
                                        for name, schema in SCHEMAS.items()]}
                elif method == "tools/call":
                    name = params.get("name")
                    args = object_field(params.get("arguments", {}), "arguments")
                    operations = {"list_sources": lambda: service.sources(args.get("q", "")),
                      "list_plugins": service.plugins, "run_plugin": lambda: service.run(args),
                      "review_question": lambda: service.council(args), "run_workflow": lambda: service.workflow(args),
                      "export_snapshot": service.export,
                      "list_resources": lambda: service.network_call("resources", args),
                      "plan_research": lambda: service.network_call("plan", args),
                      "start_campaign": lambda: service.network_call("campaign", args),
                      "network_status": lambda: service.network_call("status"),
                      "advance_campaign": lambda: service.network_call("tick", args),
                      "cancel_campaign": lambda: service.network_call("cancel_campaign", args),
                      "discover_resources": lambda: service.network_call("discover", args)}
                    operations.update({
                      'harness_status': service.harnesses_status,
                      'harness_runs': service.harnesses_runs,
                      'run_harness_task': lambda: service.harnesses_run(args),
                      'brain_status': lambda: service.brain_call('status'),
                      'start_brain_session': lambda: service.brain_call('start', args),
                      'get_brain_session': lambda: service.brain_call('session', args),
                      'advance_brain_session': lambda: service.brain_call('tick', args),
                      'cancel_brain_session': lambda: service.brain_call('cancel', args),
                      "federation_status": lambda: service.federation_call("status"),
                      "discover_agent_candidates": lambda: service.federation_call("discover", args),
                      "draft_contribution_proposal": lambda: service.federation_call("proposal", args),
                      "search_agent_directory": lambda: service.society_call("directory", args),
                      "society_status": lambda: service.society_call("status"),
                      "list_research_agents": lambda: service.society_call("agents", args),
                      "route_research": lambda: service.society_call("route", args),
                      "start_research_mission": lambda: service.society_call("mission", args),
                      "advance_research_mission": lambda: service.society_call("tick", args),
                      "cancel_research_mission": lambda: service.society_call("cancel", args),
                      "record_research_claim": lambda: service.society_call("claim", args),
                    })
                    if name not in operations:
                        raise ServiceError("Unknown tool")
                    try:
                        from .service import validate_parameters
                        validate_parameters(args, SCHEMAS[name])
                        payload = operations[name]()
                        failed = isinstance(payload, dict) and payload.get("status") == "failed"
                        result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, allow_nan=False)}], "isError": failed}
                    except ServiceError as exc:
                        result = {"content": [{"type": "text", "text": exc.message}], "isError": True}
                else:
                    response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
                    raise _Response(response)
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except _Response as exc:
            response = exc.response
        except ServiceError as exc:
            code = -32700 if exc.code in ("invalid_json", "parse_error") else -32602
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": exc.message}}
        except Exception:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "Internal error"}}
        if not notification:
            writer.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
            writer.flush()


class _Response(Exception):
    def __init__(self, response):
        self.response = response

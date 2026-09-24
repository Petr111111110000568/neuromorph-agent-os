# v0.5 distributed research contract

Core remains Python 3.11+ stdlib. Root owns integration in workbench/service.py, server.py, __main__.py, __init__.py; release/docs. Independent agents own only their modules below.

## Durable coordinator (network_queue agent)
`workbench/network/queue.py`: `Queue(path)` (SQLite WAL, atomic transactions across processes). Methods:
- submit(kind, payload, capabilities=None, idempotency_key=None, max_attempts=3) -> public job dict.
- register_worker(worker_id, capabilities) -> public worker dict.
- claim(worker_id, lease_seconds=60) -> job dict with lease_token, or None. Match required capabilities; reclaim expired jobs; bounded attempts.
- heartbeat(job_id, worker_id, lease_token, lease_seconds=60) -> job dict.
- finish(job_id, worker_id, lease_token, result=None, error=None) -> job dict. Reject stale owners; duplicate completion safely idempotent.
- cancel(job_id) -> job dict; jobs() -> list; workers() -> list; get(job_id) -> dict; close().
Job public fields id,kind,payload,capabilities,status,attempts,max_attempts,worker_id,created_at,updated_at,result,error. Lease token must never appear in jobs/get/export except claim result. Exceptions ValueError/KeyError.
Allowed kinds discovery, simulation. Capabilities discovery, simulation. Worker results are reported output, not trusted scientific validation. Limits payload/result sizes, finite JSON; worker identity shape and capability validation.

## Discovery/catalog/planning (discovery agent)
`workbench/network/discovery.py`: `discover(query, providers=None, limit=8, offline=False, root=None)` -> {query,items:[resource],errors:[],requests:int,mode}. Only public metadata via fixed provider endpoints (Europe PMC and Crossref minimum; no arbitrary URL fetch), timeout and bounded payload. Metadata unverified. No following downloaded code/instructions. `catalog(root, extra=None)` -> list resources normalized from data sources and data/resource_catalog.json. `rank_resources(question, resources)` -> ranked list records with match explanation and computability. `inspect_local(directory)` -> capped metadata inspection of explicit local manifests/files, no recursive external paths/no executing code. Resources id,title,url,description,capabilities:list,kind,access,status,provenance,limitations; aliases okay but maintain these.
`plan(question, resources)` -> {question,mode:'rule_based',steps:[],ranked_resources:[],gaps:[],limits:[]}. Biological goal decomposed into evidence/model-validation tasks, never intervention/genome editing instructions. Explicit capability vs available adapter.

## Transport/executor (network_transport agent)
`workbench/network/transport.py`: `make_hub(queue, host='127.0.0.1', port=8766, token=None, certfile=None, keyfile=None)` -> ThreadingHTTPServer. POST /v1/register {worker_id,capabilities}; /v1/claim {worker_id}; /v1/heartbeat {job_id,worker_id,lease_token}; /v1/finish {job_id,worker_id,lease_token,result?,error?}. GET /v1/health. All require Bearer token (env META_HUB_TOKEN via CLI). No wildcard CORS; payload bounds. Worker token role cannot submit/delete jobs. Bind beyond loopback only with TLS. Worker IDs tied to separately configured credentials? minimum token+lease fenced ownership; document trusted-worker domain.
`workbench/network/worker.py`: `run_worker(url, token, worker_id, root, data_dir=None, once=False, max_jobs=0, poll_seconds=1, capabilities=None)` loop registers, claims, heartbeats, invokes pinned existing `Service.run` for simulation payload {plugin_id,parameters}; invokes discovery.discover(**payload) for discovery. Only two handlers, no arbitrary command/URL code. token is never printed or persisted. Retry bounded network errors; timeout+stop. CLI lives `python -m workbench.network.worker` with --hub --id --once --max-jobs --poll-seconds --capabilities and token from env. Real worker processes; no mocked completion. Imports can be lazy for independent work.

## Integration/campaigns/account profiles (root)
Root adds `network/control.py` exposing NetworkControl(root,state_dir), resources/discover/plan/campaign_tick/status/accounts methods; a durable campaign performs bounded iterations, queues search queries and synthetic/control calculations, adds newly found metadata to catalog, identifies next evidence gaps. No self-modifying code/installing discovered plugins.
Account registry stores provider name, domain, auth mode, env var name, permission scopes, setup URL, status. Secrets only referenced by env names. Credential presence is not verified remote authentication. Registration handoffs persisted; no fake online registrations or bypasses.

## Local UI API contract (root integration, web agent)
All existing APIs remain. GET /api/network -> {workers:[],jobs:[],campaigns:[],resources_count,mode,limitations:[]}
GET /api/resources?q= -> {items:resources}
GET /api/campaigns -> {items:[]}
GET /api/accounts -> {items:profiles,requests:[]}
POST /api/discover {query,providers?:[],limit?:8,offline?:false} -> queued job
POST /api/plan {question} -> plan dict
POST /api/campaign {question,max_iterations?:2,max_jobs?:6,online?:false,simulate?:true} -> campaign dict
POST /api/campaign/tick {id} -> campaign dict (advance based on completed jobs, enqueue next bounded stage)
POST /api/campaign/cancel {id} -> campaign dict
POST /api/job/cancel {id} -> job dict
POST /api/accounts {provider,domain,auth_mode,credential_env?,scopes?:[],setup_url?} -> profile
POST /api/accounts/request {provider,reason} -> persisted handoff record (status requires_user_action / no_registration_required, not success).
POST /api/local-resources {path} -> inspection results saved to catalog; root/local API only.
GET /api/network/export -> redacted JSON snapshot (no tokens/lease secrets).
Campaign id,question,status,iteration,max_iterations,max_jobs,jobs:[],plan,events:[],created_at. Terminal statuses completed,failed,budget_exhausted,cancelled. Online metadata requests require explicit online true. UI polling can call ticks but autonomous progression also `python -m workbench daemon` (root).

Frontend adds 'Сеть и ресурсы' 'Кампании' 'Подключения' or separate network.html with full navigation; existing web no rewritten core. Strict CSP external JS/CSS, textContent only. Display pending/inaccessible/unknown distinctly, never treat metadata search as validated science. No credentials fields (only environment variable names).

Recovery addition: Queue.by_idempotency_key(key) returns a redacted existing job or None. Campaign cancellation checks deterministic keys for every bounded iteration, including jobs submitted before a control-DB interruption. Result review validates metadata envelopes and builtin simulation hashes before marking an iteration complete.

# Meta-Harness v0.5 core runtime contract
Local web research console, Python 3.11+ standard library; no required pip dependencies. The restored srf/ is the original v0.3 prototype and is not the execution engine. Russian interface. Computational research and simulation only.

GET /api/status -> {name,version,mode,counts:{sources,runs,plugins},environment:{...},limitations:[]}
GET /api/sources?q= -> {items:[{id,title,url,category,maturity,integration,limitations,status}]}; sources combine data/*_sources.json.
POST /api/sources {title,url,category,summary} -> persisted source.
GET /api/plugins -> {items:[{id,name,description,parameters:JSONSchema,status,limitations:[]}]}
POST /api/run {plugin_id,parameters:{}} -> {id,plugin_id,status,parameters,result:{summary,metrics:[{label,value,unit?}],series:[{name,points:[{x,y}]}],table:[],limitations:[]},created_at,provenance:{...},error?}
GET /api/runs -> {items:[runs]}
GET /api/advisors -> {items:[{id,name,focus,kind:'rule_based'}]}
POST /api/council {question,source_ids?:[]} -> {id,question,mode:'rule_based',verdict,advisors:[{id,name,assessment,actions:[]}],source_ids:[],next_steps:[],created_at}
GET /api/council -> {items:[]}
POST /api/workflow {question,plugin_id,parameters,source_ids?:[]} -> {id,status,council,run,summary,created_at}
GET /api/workflows -> {items:[]}
GET /api/environment -> data/environment_status.json
GET /api/roadmap -> data/roadmap.json {items:[]}
GET /api/audit -> {items:[{id,event,timestamp,details,prev_hash,hash}],valid:bool}
GET /api/export -> downloadable JSON snapshot (version,collections,sources,audit)
GET /api/report -> downloadable Markdown report
Error HTTP4xx/5xx {error:{message,code}}

workbench/plugins.py exports list_plugins()->list and execute(plugin_id,parameters)->dict. Plugins run through `python -I <root>/plugin_worker.py <plugin_id>`; stdin is a parameters object and stdout is {result:...}. Only registered, trusted builtins with pinned hashes execute. Process resource limits are not a sandbox for arbitrary code.

The council uses only explicitly supplied source_ids; omitting the field attaches no sources. The API accepts up to 100 identifiers and rejects unknown IDs; the UI limits selection to 20 for usability. Source attachment records provenance and does not automatically read or validate a paper.

Completed and failed runs are persisted; there is no durable job queue or automatic recovery of an interrupted active process. SQLite state survives normal restarts. JSON export is an archive/report format, not an import format in this release.

The frontend uses external static JS/CSS with no CDN dependencies. External hosting, LLMs, QPU and container isolation require separately verified integrations.

Distributed extension: see NETWORK_CONTRACT.md. CLI `local-network` starts hub, daemon and2 worker processes by default.

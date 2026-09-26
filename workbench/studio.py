"""Persistent research workspace; records are human input, never executable jobs."""
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit
from .store import canonical, now


FIELDS = {
    'project': {'title', 'body'},
    'task': {'title', 'body', 'project_id', 'question', 'criteria', 'status'},
    'artifact': {'title', 'body', 'project_id', 'source_url', 'evidence'},
}
ID = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')


def invalid(message, code='invalid_request', status=400):
    from .service import ServiceError
    return ServiceError(message, code, status)


class StudioWorkspace:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.executescript('''
              CREATE TABLE IF NOT EXISTS studio_records (
                owner TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                version INTEGER NOT NULL, payload TEXT NOT NULL,
                request_key TEXT, request_hash TEXT,
                PRIMARY KEY(owner,id), UNIQUE(owner,request_key));
              CREATE TABLE IF NOT EXISTS studio_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                payload TEXT NOT NULL);
            ''')

    def snapshot(self, owner='local'):
        with self.store.lock:
            records = self.store.db.execute(
                'SELECT kind,payload FROM studio_records WHERE owner=? ORDER BY rowid DESC', (owner,)).fetchall()
            events = self.store.db.execute(
                'SELECT payload FROM studio_events WHERE owner=? ORDER BY seq DESC LIMIT 300', (owner,)).fetchall()
        return {'schema_version': 1, 'projects': [json.loads(p) for k,p in records if k == 'project'],
                'tasks': [json.loads(p) for k,p in records if k == 'task'],
                'artifacts': [json.loads(p) for k,p in records if k == 'artifact'],
                'events': [json.loads(p[0]) for p in events], 'observed_at': now(),
                'capabilities': {'cloud_auth': owner != 'local', 'model_calls': False,
                    'execution': 'existing_core', 'daily_budget': 0,
                    'notice': 'Workspace records do not dispatch models or native chats.'}}

    def save(self, data, owner='local'):
        if not isinstance(data, dict):
            raise invalid('Expected an object')
        kind = data.get('kind')
        if not isinstance(kind, str) or kind not in FIELDS:
            raise invalid('Unknown entity kind')
        allowed = FIELDS[kind] | {'kind', 'id', 'version', 'idempotency_key'}
        if set(data) - allowed:
            raise invalid('Unknown entity fields')
        clean = {}
        for key in FIELDS[kind]:
            value = data.get(key, '')
            maximum = 200 if key == 'title' else 2048 if key == 'source_url' else 12000
            if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
                raise invalid('Invalid field: ' + key)
            clean[key] = value.strip()
        if not clean['title']:
            raise invalid('Title is required')
        if kind != 'project' and not ID.fullmatch(clean['project_id']):
            raise invalid('Project is required')
        if kind == 'task':
            clean['status'] = clean['status'] or 'planned'
            if clean['status'] not in {'planned','working','review','done','paused'}:
                raise invalid('Unknown task status')
        if kind == 'artifact':
            clean['evidence'] = clean['evidence'] or 'unreviewed'
            if clean['evidence'] not in {'unreviewed','reviewed','tested'}:
                raise invalid('Unknown evidence status')
            if clean['source_url']:
                try:
                    url = urlsplit(clean['source_url'])
                    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.port not in (None,443):
                        raise ValueError()
                except ValueError:
                    raise invalid('Source URL must be an HTTPS link without credentials')
        entity_id = data.get('id')
        creating = entity_id is None
        if not creating and (not isinstance(entity_id, str) or not ID.fullmatch(entity_id)):
            raise invalid('Invalid id')
        version = data.get('version')
        if not creating and (type(version) is not int or version < 1):
            raise invalid('An existing version is required')
        key = data.get('idempotency_key')
        if creating and (not isinstance(key,str) or not ID.fullmatch(key)):
            raise invalid('An idempotency key is required')
        if not creating and key is not None:
            raise invalid('Idempotency keys apply to creation only')
        digest = hashlib.sha256(canonical({'kind':kind, **clean}).encode()).hexdigest()
        db = self.store.db
        with self.store.lock:
            db.execute('BEGIN IMMEDIATE')
            try:
                if creating:
                    previous = db.execute('SELECT request_hash,payload FROM studio_records WHERE owner=? AND request_key=?', (owner,key)).fetchone()
                    if previous:
                        if previous[0] != digest:
                            raise invalid('Idempotency key already used for another request', 'idempotency_conflict',409)
                        db.commit()
                        return json.loads(previous[1])
                    count = db.execute('SELECT COUNT(*) FROM studio_records WHERE owner=?',(owner,)).fetchone()[0]
                    if count >= 2000:
                        raise invalid('Workspace record limit reached', 'workspace_limit',409)
                else:
                    previous = db.execute('SELECT kind,version,payload FROM studio_records WHERE owner=? AND id=?', (owner,entity_id)).fetchone()
                    if not previous or previous[0] != kind:
                        raise invalid('Entity not found', 'not_found',404)
                    if previous[1] != version:
                        raise invalid('Record changed. Reload before editing.', 'version_conflict',409)
                if kind != 'project' and not db.execute('SELECT 1 FROM studio_records WHERE owner=? AND id=? AND kind=?',(owner,clean['project_id'],'project')).fetchone():
                    raise invalid('Project not found', 'not_found',404)
                timestamp = now()
                result = {**clean, 'id': entity_id or uuid.uuid4().hex, 'version':1 if creating else version+1,
                          'created_at': timestamp if creating else json.loads(previous[2])['created_at'], 'updated_at':timestamp}
                if creating:
                    db.execute('INSERT INTO studio_records VALUES(?,?,?,?,?,?,?)',(owner,kind,result['id'],1,canonical(result),key,digest))
                else:
                    db.execute('UPDATE studio_records SET version=?,payload=? WHERE owner=? AND id=? AND version=?',(result['version'],canonical(result),owner,entity_id,version))
                event = {'id':uuid.uuid4().hex,'entity_id':result['id'],'kind':kind,'event':'created' if creating else 'updated',
                         'version':result['version'],'at':timestamp,'actor':'operator','model_generated':False}
                db.execute('INSERT INTO studio_events(owner,payload) VALUES(?,?)',(owner,canonical(event)))
                db.execute('DELETE FROM studio_events WHERE owner=? AND seq NOT IN (SELECT seq FROM studio_events WHERE owner=? ORDER BY seq DESC LIMIT 10000)',(owner,owner))
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

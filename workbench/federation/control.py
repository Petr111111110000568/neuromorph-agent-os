"""Local administrator facade. Discovery metadata never grants membership."""
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from ..store import canonical, now
from ..network.control import fields, text, integer
from .exchange import Exchange
from . import discovery


class Federation:
    def __init__(self, root, state_dir):
        self.root, self.state_dir = Path(root), Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.exchange = Exchange(self.state_dir / 'federation_exchange.sqlite3')
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.state_dir / 'federation_discovery.sqlite3'), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS searches(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
        ''')

    def close(self):
        with self.lock:self.db.close()
        self.exchange.close()

    def candidates(self):
        with self.lock:
            rows=self.db.execute('SELECT payload FROM candidates ORDER BY rowid DESC LIMIT 1000').fetchall()
        return {'items':[json.loads(row[0]) for row in rows]}

    def _save_candidates(self, items):
        accepted=[]
        with self.lock, self.db:
            for raw in items[:32]:
                if not isinstance(raw,dict):continue
                item=dict(raw)
                key=str(item.get('id',''))+'|'+str(item.get('url',''))+'|'+str(item.get('provenance',{}).get('provider',''))
                item['catalog_id']=item.get('id','')
                item['id']='candidate_'+hashlib.sha256(key.encode()).hexdigest()[:24]
                item['name']=str(item.get('name',item.get('title','Unnamed resource')))[:240]
                item['imported_at']=now()
                item['membership']='not_joined'
                self.db.execute('INSERT INTO candidates VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(item['id'],canonical(item)))
                accepted.append(item)
            self.db.execute('DELETE FROM candidates WHERE rowid NOT IN (SELECT rowid FROM candidates ORDER BY rowid DESC LIMIT 1000)')
        return accepted

    def discover(self, body):
        fields(body, {'query','providers','limit','online','data_class'})
        if not isinstance(body.get('online',False),bool):raise ValueError('online must be Boolean')
        result=discovery.discover(query=body.get('query'),providers=body.get('providers'),limit=body.get('limit',5),online=body.get('online',False),root=self.root,data_class=body.get('data_class','public'))
        result['items']=self._save_candidates(result.get('items',[]))
        result['id']='search_'+uuid.uuid4().hex
        with self.lock,self.db:
            self.db.execute('INSERT INTO searches VALUES(?,?,?)',(result['id'],now(),canonical(result)))
            self.db.execute('DELETE FROM searches WHERE rowid NOT IN (SELECT rowid FROM searches ORDER BY rowid DESC LIMIT 100)')
        return result

    def card(self, body):
        fields(body,{'url','tor_proxy'})
        item=discovery.inspect_card(body.get('url'),tor_proxy=body.get('tor_proxy') or None)
        return self._save_candidates([item])[0]

    def status(self):
        with self.lock:
            counts={name:self.db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ['candidates','searches','proposals']}
            latest=self.db.execute('SELECT payload FROM searches ORDER BY rowid DESC LIMIT 1').fetchone()
        return {'version':'0.7.0','discovery':counts,'latest_search':json.loads(latest[0]) if latest else None,'exchange':self.exchange.status(),
          'limitations':['Каталоги и карточки содержат заявления авторов; организация и научная компетентность не подтверждены.',
            'Отправка приглашений, регистрация на внешних платформах и полный поиск всей сети не выполняются.',
            'Tor требует отдельно работающий локальный SOCKS5 proxy и известный адрес; живой onion-сервис не входит в поставку.',
            'Доступ обменивается только на принятый результат; кредиты не деньги. Персональные геномы не входят в обмен.']}

    def proposal(self, body):
        fields(body,{'candidate_id','offer_id'})
        cid=text(body.get('candidate_id'),'candidate_id',100)
        oid=text(body.get('offer_id'),'offer_id',100)
        candidate=next((x for x in self.candidates()['items'] if x['id']==cid),None)
        offer=next((x for x in self.exchange.list_offers(public=True)['items'] if x.get('offer_id',x.get('id'))==oid),None)
        if candidate is None or offer is None:raise KeyError('Candidate or available offer not found')
        capabilities=candidate.get('capabilities',[])
        if not isinstance(capabilities,list):capabilities=[]
        capabilities=[str(x)[:150] for x in capabilities[:30]]
        terms=offer.get('terms',offer)
        offertext=canonical(terms).lower()
        matched=[c for c in capabilities if any(t in offertext for t in re.findall(r'[\w-]{4,}',c.lower()))]
        resources=self.exchange.list_resources(public=True)['items']
        benefit='; '.join(str(r.get('title',''))[:120]+' ('+str(r.get('cost_credits',0))+' кр.)' for r in resources[:3]) or 'Ресурсы для обмена ещё не опубликованы; не обещать несуществующий доступ.'
        message=(f"Предложение Meta-Harness для {candidate['name']}.\n"
          f"Задача: {terms.get('title',offer.get('title',''))}.\n"
          f"Ожидаемый результат: {terms.get('description',offer.get('description',''))}.\n"
          f"После приёмки: {terms.get('reward_credits',offer.get('reward_credits',0))} непередаваемых кредитов доступа.\n"
          f"Доступные материалы: {benefit}\n"
          'Участие добровольное, с согласием оператора. До начала согласуются замороженные условия и лимит. '
          'Результат проверяет администратор; время само по себе не даёт вознаграждения. '
          'Предоставляются только разрешённые исследовательские материалы, без персональных геномов.\n'
          f"Идентификатор предложения: {oid}; хеш условий: {offer.get('terms_hash','')}.\n"
          'Для допуска оператор проекта передаёт одноразовое приглашение защищённым согласованным способом.')
        result={'id':'proposal_'+uuid.uuid4().hex,'candidate_id':cid,'offer_id':oid,'offer':offer,'matched_capabilities':matched,
          'matching_method':'lexical_overlap_not_competence_validation','message':message,'delivery_status':'draft_not_sent',
          'declared_opt_in':bool(candidate.get('collaboration',{}).get('opt_in',False)),
          'warning':'Карточка и черновик не означают согласие или присоединение агента.'}
        with self.lock,self.db:
            self.db.execute('INSERT INTO proposals VALUES(?,?,?)',(result['id'],now(),canonical(result)))
            self.db.execute('DELETE FROM proposals WHERE rowid NOT IN (SELECT rowid FROM proposals ORDER BY rowid DESC LIMIT 100)')
        return result

    def export(self):
        with self.lock:
            proposals=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM proposals ORDER BY rowid')]
        return {'status':self.status(),'candidates':self.candidates()['items'],'proposals':proposals,'exchange':self.exchange.export(),
          'privacy_note':'Includes public queries and administrator-authored proposal text; no membership tokens or protected resource content.'}

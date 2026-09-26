"""Local UI bridge to one pinned, finite offline council at a time."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading

from .autonomy.daemon import _safe_path, _job_lock
from .autonomy.local_review import _save_proposal
from .service import ServiceError, text_field


@contextmanager
def _admission_lock(path):
    """Expose contention as retryable without masking errors in the body."""
    lock = _job_lock(path)
    try:
        lock.__enter__()
    except ValueError as exc:
        if str(exc) != 'Schedule is already running':
            raise
        raise ServiceError('Offline request admission is busy','offline_busy',409) from exc
    try:
        yield
    finally:
        lock.__exit__(None,None,None)


class OfflineControl:
    def __init__(self, root, data_dir, *, runner=None):
        self.root = Path(root)
        self.folder = _safe_path(data_dir,directory=True) / 'offline-council'
        _safe_path(self.folder,directory=True)
        self.folder.mkdir(exist_ok=True)
        _safe_path(self.folder, directory=True)
        self.lock = threading.Lock()
        self.thread = None
        self.current_folder = None
        self.closed = False
        self.runner = runner

    def snapshot(self):
        jobs = []
        for folder in sorted(self.folder.iterdir(), key=lambda p:p.name):
            if not folder.is_dir() or len(folder.name) != 64:
                continue
            _safe_path(folder, directory=True)
            request = self._read(folder / 'request.json')
            result = self._read(folder / 'council-receipt.json')
            state = self._read(folder / 'state.json')
            jobs.append({'id':folder.name, 'question':request.get('question',''),
                         'status':result.get('status',state.get('status','reserved')),
                         'attempts':state.get('attempts',0), 'result':result})
        return {'schema_version':1, 'model':'Qwen3-14B-Q4_K_M',
                'profile':'llama.cpp b11146 / Windows Vulkan0',
                'model_files_present':(self.root/'runtime/local-model/manifest.json').is_file(),
                'running':bool(self.thread and self.thread.is_alive()), 'jobs':jobs,
                'notice':'Три роли одной локальной модели; ответы требуют проверки. Это не независимые модели.'}

    @staticmethod
    def _read(path):
        path = _safe_path(path)
        if not path.exists():
            return {}
        if path.stat().st_size > 192*1024:
            raise ServiceError('Offline record too large','invalid_offline_record',500)
        value=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value,dict):
            raise ServiceError('Invalid offline record','invalid_offline_record',500)
        return value

    def start(self, data):
        if type(data) is not dict or set(data)!={'question','request_id','public_data_confirmed'} or data['public_data_confirmed'] is not True:
            raise ServiceError('Confirm public data and supply question/request_id')
        question=text_field(data['question'],'question',2000)
        key=text_field(data['request_id'],'request_id',80)
        if not all(c.isalnum() or c=='-' for c in key):
            raise ServiceError('Invalid request ID')
        job=hashlib.sha256(key.encode()).hexdigest()
        folder=self.folder/job
        request={'question':question,'request_id':key,'public_data_confirmed':True}
        with self.lock, _admission_lock(self.folder/'admission.lock'):
            if self.closed:
                raise ServiceError('Offline controller is closed','offline_closed',409)
            _safe_path(folder,directory=True)
            if folder.exists():
                if self._read(folder/'request.json')!=request:
                    raise ServiceError('Request ID already has another question','idempotency_conflict',409)
                return {'id':job,'status':'existing','duplicate_suppressed':True}
            if self.thread and self.thread.is_alive():
                raise ServiceError('One offline council is already running','offline_busy',409)
            if len([p for p in self.folder.iterdir() if p.is_dir()])>=24:
                raise ServiceError('Local council history limit reached','offline_limit',409)
            if not (self.root/'runtime/local-model/manifest.json').is_file():
                raise ServiceError('Pinned local model is not configured','model_not_configured',503)
            process_lock=_job_lock(self.folder/'model.lock')
            try:
                process_lock.__enter__()
            except (ValueError,OSError) as exc:
                raise ServiceError('One offline council is already running','offline_busy',409) from exc
            # A durable request is reserved before the thread starts. A crash never resends it.
            try:
                folder.mkdir()
                _save_proposal(folder/'request.json',request)
                self.current_folder=folder
                self.thread=threading.Thread(target=self._run,args=(folder,question,job,process_lock),daemon=True)
                self.thread.start()
            except Exception:
                process_lock.__exit__(None,None,None)
                raise
            return {'id':job,'status':'reserved','duplicate_suppressed':False}

    def _run(self, folder, question, job, process_lock):
        try:
            from .offline_council import run_council
            runner=self.runner or run_council
            result=runner(question,root=self.root,
                    executable=self.root/'runtime/local-model/llama-b11146-vulkan/llama-cli.exe',
                    model=self.root/'runtime/local-model/Qwen3-14B-Q4_K_M.gguf',
                    manifest=self.root/'runtime/local-model/manifest.json',
                    output_dir=folder,state_file=folder/'state.json',stop_file=folder/'STOP',
                    project_id='local-ui',task_id=job,model_name='Qwen3-14B-Q4_K_M',max_tokens=384,timeout=300)
            if not (folder/'council-receipt.json').exists():
                if not isinstance(result,dict) or not isinstance(result.get('status'),str):
                    raise ValueError('Invalid council result')
                _save_proposal(folder/'council-receipt.json',result)
        except Exception:
            if not (folder/'council-receipt.json').exists():
                _save_proposal(folder/'council-receipt.json',{'schema_version':1,'status':'failed',
                    'notice':'Запуск не завершён; подробности и входы не включены в ошибку. Повтор автоматически не выполняется.'})
        finally:
            process_lock.__exit__(None,None,None)

    def stop(self):
        with self.lock:
            for folder in self.folder.iterdir():
                if folder.is_dir() and (folder/'request.json').exists() and not (folder/'council-receipt.json').exists():
                    stop=_safe_path(folder/'STOP')
                    if not stop.exists():
                        stop.write_text('stop',encoding='utf-8')
        return {'stop_requested':True}

    def close(self):
        """Stop only work launched by this controller, then bound shutdown wait.

        A status-only MCP session must not stop an independent UI controller's
        job. Explicit stop() remains the operator's data-directory-wide action.
        """
        with self.lock:
            self.closed = True
            thread = self.thread
            if thread is not None and thread.is_alive() and self.current_folder is not None:
                stop = _safe_path(self.current_folder / 'STOP')
                if not stop.exists():
                    stop.write_text('stop', encoding='utf-8')
        if thread is not None and thread.ident is not None and thread is not threading.current_thread():
            thread.join(timeout=6)

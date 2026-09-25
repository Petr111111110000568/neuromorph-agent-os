"""Runs reviewed builtins with resource budgets; this is NOT a security sandbox."""
import json
import os
import sys
from pathlib import Path
if os.name=='posix':
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(5,6))
    resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,)*2)
    resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,)*2)
    resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from workbench.plugins import execute
try:
    # -I ignores PYTHONIOENCODING; the protocol always uses explicit UTF-8 bytes.
    raw=sys.stdin.buffer.read(1000001)
    if len(raw)>1000000:raise ValueError('Input too large')
    parameters=json.loads(raw.decode("utf-8"),parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
    result=execute(sys.argv[1],parameters)
    sys.stdout.buffer.write((json.dumps({'result':result},ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8'))
except (ValueError,KeyError,IndexError) as error:
    sys.stderr.buffer.write((json.dumps({'error':str(error)},ensure_ascii=False)+'\n').encode('utf-8'))
    sys.exit(2)

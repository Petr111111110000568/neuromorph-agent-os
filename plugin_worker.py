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
    raw=sys.stdin.buffer.read(1000001)
    if len(raw)>1000000:raise ValueError('Input too large')
    parameters=json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
    result=execute(sys.argv[1],parameters)
    print(json.dumps({'result':result},ensure_ascii=False,allow_nan=False))
except (ValueError,KeyError,IndexError) as error:
    print(json.dumps({'error':str(error)},ensure_ascii=False),file=sys.stderr)
    sys.exit(2)

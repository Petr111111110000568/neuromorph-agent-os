"""Create an isolated stdlib core environment without downloads or secrets."""
import json
import os
import subprocess
import sys
import venv
from pathlib import Path
root=Path(__file__).resolve().parents[1]
if sys.version_info<(3,11):raise SystemExit('Python 3.11+ required')
venv_path=root/'.venv'
if not (venv_path/'pyvenv.cfg').exists():venv.EnvBuilder(with_pip=True).create(venv_path)
python=venv_path/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
result=subprocess.run([str(python),'-m','workbench','doctor'],cwd=root)
raise SystemExit(result.returncode)

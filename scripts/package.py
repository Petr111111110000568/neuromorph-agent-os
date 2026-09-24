"""Build a reproducible source archive; excludes local state and credentials."""
import hashlib
import json
import zipfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
destination=root.parent/'dist';destination.mkdir(exist_ok=True)
output=destination/'Meta-Harness-v0.10.zip'
excluded={'.venv','__pycache__','.git','state','runtime','node_modules'}
files=[]
for p in sorted(root.rglob('*')):
 if not p.is_file() or p.is_symlink():continue
 rel=p.relative_to(root)
 if any(x in excluded for x in rel.parts) or p.suffix in ('.pyc','.sqlite3','.sqlite','.db') or p.name.startswith('.env'):continue
 files.append(p)
manifest={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.name!='BUILD_MANIFEST.json'}
(root/'BUILD_MANIFEST.json').write_text(json.dumps({'files':manifest},indent=2)+'\n')
if root/'BUILD_MANIFEST.json' not in files:files.append(root/'BUILD_MANIFEST.json')
with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
 for p in sorted(files):
  info=zipfile.ZipInfo('Meta-Harness-v0.10/'+p.relative_to(root).as_posix(),(2026,9,24,0,0,0))
  info.compress_type=zipfile.ZIP_DEFLATED
  info.external_attr=(0o755 if p.suffix=='.sh' else 0o644)<<16
  z.writestr(info,p.read_bytes())
checksum=hashlib.sha256(output.read_bytes()).hexdigest()
(output.parent/(output.name+'.sha256')).write_text(checksum+'  '+output.name+'\n')
print(json.dumps({'file':str(output),'bytes':output.stat().st_size,'sha256':checksum,'files':len(files)}))

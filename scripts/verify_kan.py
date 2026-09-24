"""Run six predeclared synthetic experiments, keeping negative comparisons."""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.kan import execute


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='examples/kan_benchmark_run.json')
    args = parser.parse_args()
    runs = []
    for task in ['additive', 'interaction']:
        for basis in ['spline', 'rbf', 'wavelet']:
            started = time.perf_counter()
            result = execute({'task': task, 'basis': basis})
            payload = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
            runs.append({'task': task, 'basis': basis, 'wall_seconds': round(time.perf_counter() - started, 4),
                         'result_sha256': hashlib.sha256(payload).hexdigest(), 'result': result})
    proof = {'schema': 'meta-harness-kan-verification/1', 'scope': 'synthetic_benchmark_not_biology',
             'design': 'six combinations declared before running; all results retained; no tuning on test',
             'runs': runs}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proof, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(path), 'runs': len(runs), 'seeds_per_run': 3,
                      'wall_seconds': [r['wall_seconds'] for r in runs]}, ensure_ascii=False))


if __name__ == '__main__':
    main()

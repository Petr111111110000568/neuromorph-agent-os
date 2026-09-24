"""Exercise the distributed web/API workflow in temporary local state."""
import http.client
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.server import make_server
from workbench.service import Service


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix="meta-harness-smoke-") as directory:
        db = Path(directory) / "state.sqlite3"
        service = Service(ROOT, db)
        server = make_server(service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, payload=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
            body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            headers = {} if body is None else {"Content-Type": "application/json"}
            connection.request("GET" if body is None else "POST", path, body=body, headers=headers)
            response = connection.getresponse()
            data, status, mime = response.read(), response.status, response.getheader("Content-Type", "")
            connection.close()
            assert status in (200, 201), (path, status, data[:200])
            return json.loads(data) if "application/json" in mime else data

        try:
            for path, minimum in (("/", 1000), ("/app.js", 1000), ("/styles.css", 1000)):
                assert len(request(path)) > minimum
            checks.append("html_js_css_served_over_http")
            status = request("/api/status")
            assert status["environment"]["builtin_integrity"]
            assert status["counts"]["plugins"] == 7
            assert any(p['id']=='structural_plasticity' for p in request('/api/plugins')['items'])
            checks.append("builtin_integrity")
            for path in ('/federation.html','/federation.js','/federation.css','/morphogenesis.html','/morphogenesis.js','/morphogenesis.css'):
                assert len(request(path)) > 300
            structural = request('/api/run', {'plugin_id':'structural_plasticity','parameters':{'replicates':1}})
            assert structural['status'] == 'completed'
            assert structural['result']['validation']['test_used_for_rewiring'] is False
            assert len(structural['result']['trials'][0]['conditions']) == 3
            checks.append('structural_plasticity_actual_http_run_and_new_pages')
            source = request("/api/sources", {"title": "Release smoke fixture", "url": "https://example.org/fixture", "summary": "Synthetic test entry, not evidence."})
            assert source["status"] == "unverified"
            outcome = request("/api/workflow", {"question": "Проверка воспроизводимого вычислительного цикла", "source_ids": [source["id"]], "plugin_id": "quantum_circuit", "parameters": {"theta": 0, "shots": 100, "seed": 11}})
            assert outcome["status"] == "completed"
            assert outcome["council"]["source_ids"] == [source["id"]]
            assert outcome["run"]["result"]["model"]["qpu_used"] is False
            checks.append("source_to_council_to_real_worker")
            export = request("/api/export")
            assert export["audit"]["valid"]
            assert len(export["collections"]["workflow"]) == 1
            assert b"quantum_circuit" in request("/api/report")
            checks.append("json_markdown_export_and_audit")
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            service.close()
        restored = Service(ROOT, db)
        try:
            assert restored.workflows()["items"][0]["id"] == outcome["id"]
            assert restored.audit()["valid"]
            checks.append("restart_preserves_records")
        finally:
            restored.close()
    print(json.dumps({"passed": True, "checks": checks, "browser_render_test": "not_performed"}, indent=2))


if __name__ == "__main__":
    main()

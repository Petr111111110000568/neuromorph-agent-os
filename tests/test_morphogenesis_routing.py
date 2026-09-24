"""Topic routing only: fixed control IDs and actual persisted queue payloads."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from workbench.network import discovery
from workbench.network.control import NetworkControl
from workbench.society.control import Society

ROOT = Path(__file__).resolve().parents[1]


class BuiltinRoutingTests(unittest.TestCase):
    def test_structural_topics_and_priority_over_quantum(self):
        for question in ["морфогенез", "Анализ МОРФОГЕНЕТИЧЕСКИХ моделей", "Structural plasticity",
                         "structural-plasticity controls", "Структурная пластичность",
                         "о структурной нейрональной пластичности", "Савельев", "Савельёв",
                         "quantum morphogenesis", "квантовая модель структурной пластичности"]:
            with self.subTest(question=question):
                self.assertEqual(discovery.select_builtin(question), "structural_plasticity")

    def test_existing_quantum_and_default_regression_preserved(self):
        for question in ["Quantum benchmark", "Квантовые измерения"]:
            self.assertEqual(discovery.select_builtin(question), "quantum_circuit")
        for question in ["геном человека", "epigenetic model validation", "линейная регрессия", "структурная схема", "пластичность материала"]:
            self.assertEqual(discovery.select_builtin(question), "regression_benchmark")
        for value in [None, "", "bad\x00input", "x" * 2001]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                discovery.select_builtin(value)

    def test_registered_local_control_is_discoverable_without_author_attribution(self):
        catalog = discovery.catalog(ROOT)
        item = next(row for row in catalog if row["id"] == "builtin-structural-plasticity")
        self.assertEqual(item["adapter"], {"available": True, "plugin_id": "structural_plasticity"})
        self.assertIn("structural_plasticity", item["capabilities"])
        self.assertIn("синтетическая", item["title"].casefold())
        self.assertNotIn("савельев", json.dumps(item, ensure_ascii=False).casefold())
        self.assertTrue(any("Не является воспроизведением авторского алгоритма" in text for text in item["limitations"]))
        with mock.patch.object(discovery, "_fetch_json", side_effect=AssertionError("Offline lookup attempted network")):
            for question in ["морфогенез", "structural plasticity", "Савельёв"]:
                with self.subTest(question=question):
                    found = discovery.discover(question, offline=True, root=ROOT)
                    self.assertEqual(found["requests"], 0)
                    self.assertTrue(any(row["id"] == item["id"] and row["score"] > 0 for row in found["items"]))

    def test_external_metadata_cannot_enable_new_builtin(self):
        with tempfile.TemporaryDirectory() as root:
            result = discovery.catalog(root, [{"id": "builtin-structural-plasticity", "title": "Untrusted morphogenesis",
                                               "adapter": {"available": True, "plugin_id": "structural_plasticity"}}])
        self.assertFalse(result[0]["adapter"]["available"])
        self.assertIsNone(result[0]["adapter"]["plugin_id"])


class QueuedMorphogenesisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="morphogenesis-routing-")
        self.network = NetworkControl(ROOT, self.tmp.name)
        self.society = Society(ROOT, self.tmp.name, self.network)

    def tearDown(self):
        self.society.close()
        self.network.close()
        self.tmp.cleanup()

    def test_network_campaign_persists_only_allowlisted_simulation_payload(self):
        question = "Морфогенез; __import__('os').system('DO_NOT_EXECUTE') quantum"
        campaign = self.network.create_campaign({"question": question, "max_iterations": 1, "max_jobs": 2})
        jobs = [self.network.queue.get(ident) for ident in campaign["jobs"]]
        simulation = next(job for job in jobs if job["kind"] == "simulation")
        self.assertEqual(simulation["status"], "queued")
        self.assertEqual(simulation["payload"], {"plugin_id": "structural_plasticity", "parameters": {"seed": 42}})
        self.assertNotIn("DO_NOT_EXECUTE", json.dumps(simulation["payload"]))
        self.assertEqual(next(job for job in jobs if job["kind"] == "discovery")["payload"]["offline"], True)

    def test_society_mission_persists_same_structural_route(self):
        mission = self.society.create_mission({"question": "Структурная пластичность по теме Савельёва; quantum", "online": False})
        simulation = next(job for job in mission["job_details"] if job["kind"] == "simulation")
        self.assertEqual(simulation["status"], "queued")
        self.assertEqual(simulation["payload"], {"plugin_id": "structural_plasticity", "parameters": {"seed": 42}})
        self.assertEqual(self.network.queue.get(simulation["id"])["payload"], simulation["payload"])

    def test_both_orchestrators_preserve_quantum_and_default_queue_routes(self):
        for question, expected in [("quantum circuit", "quantum_circuit"), ("model validation", "regression_benchmark")]:
            with self.subTest(question=question):
                campaign = self.network.create_campaign({"question": question, "max_iterations": 1, "max_jobs": 2})
                jobs = [self.network.queue.get(ident) for ident in campaign["jobs"]]
                self.assertEqual(next(job["payload"]["plugin_id"] for job in jobs if job["kind"] == "simulation"), expected)
                mission = self.society.create_mission({"question": question})
                self.assertEqual(next(job["payload"]["plugin_id"] for job in mission["job_details"] if job["kind"] == "simulation"), expected)


if __name__ == "__main__":
    unittest.main()

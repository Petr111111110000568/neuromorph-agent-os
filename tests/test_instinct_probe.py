"""Offline controls for the optional pinned Instinct heuristic fixture."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import instinct_local_probe as probe


def synthetic_wheel(extra=None, duplicate=False):
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w') as archive:
        for name in probe.MODULES:
            archive.writestr(name.replace('.', '/') + '.py', '# fixture source\n')
        if extra:
            archive.writestr(extra, 'raise RuntimeError("must not load")\n')
        if duplicate:
            # Deliberate duplicate fixture; zipfile warns, loader must refuse.
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                archive.writestr('reflex/primitives.py', '# duplicate\n')
    return target.getvalue()


class InstinctProbeTests(unittest.TestCase):
    def test_plan_has_no_download_or_subprocess(self):
        with patch.object(probe, 'download') as fetch, patch.object(probe.subprocess, 'run') as run, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(probe.main([]), 0)
        fetch.assert_not_called()
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['status'], 'plan_only')

    def test_execution_requires_cloud_before_creating_output(self):
        with patch.object(probe.sys, 'platform', 'win32'), patch.object(probe, 'fresh_output') as output:
            with self.assertRaisesRegex(ValueError, 'authorized_linux_cloud_required'):
                probe.main(['--execute'])
        output.assert_not_called()

    def test_config_pin_matches_executable_constants(self):
        config = json.loads((probe.ROOT / 'config/instinct_harness.json').read_text(encoding='utf-8'))
        self.assertEqual(config['wheel_url'], probe.WHEEL_URL)
        self.assertEqual(config['wheel_sha256'], probe.WHEEL_SHA)
        self.assertFalse(config['automatic_routing'])
        self.assertEqual(config['cloud_instinct_affiliation'], 'unverified')

    def test_digest_is_checked_before_zip_loading(self):
        with patch.object(probe.zipfile, 'ZipFile') as reader:
            for raw in (b'not a zip', b'x' * (probe.CAP + 1), 'wrong type'):
                with self.assertRaisesRegex(ValueError, 'wheel_digest_mismatch'):
                    probe.inspect_wheel(raw)
        reader.assert_not_called()

    def test_only_three_reviewed_modules_are_returned_without_initializers(self):
        raw = synthetic_wheel('reflex/__init__.py')
        with patch.object(probe, 'WHEEL_SHA', hashlib.sha256(raw).hexdigest()):
            loaded = probe.inspect_wheel(raw)
        self.assertEqual(tuple(loaded), probe.MODULES)
        self.assertNotIn('must not load', ''.join(loaded.values()))

    def test_duplicate_reviewed_member_is_refused(self):
        raw = synthetic_wheel(duplicate=True)
        with patch.object(probe, 'WHEEL_SHA', hashlib.sha256(raw).hexdigest()):
            with self.assertRaisesRegex(ValueError, 'wheel_member_missing_or_duplicate'):
                probe.inspect_wheel(raw)

    def test_reference_abstains_for_absent_or_ambiguous_labels(self):
        self.assertEqual(probe.reference('git commit'), 'abstain')
        self.assertEqual(probe.reference('code and search'), 'abstain')
        self.assertEqual(probe.reference('web search'), 'search')
        self.assertEqual(probe.reference('Найди статьи'), 'abstain')

    def test_redirect_and_worker_network_process_effects_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'download_redirect_refused'):
            probe.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.org')
        for event in ('socket.connect', 'socket.__new__', 'subprocess.Popen', 'os.system'):
            with self.assertRaisesRegex(RuntimeError, 'fixture_external_effect_refused'):
                probe.deny_effects(event, ())

    def test_output_is_fresh_and_confined_to_repo_runtime(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(probe, 'ROOT', Path(folder)):
            root = Path(folder)
            self.assertEqual(probe.fresh_output('runtime/probe'), root / 'runtime/probe')
            for bad in ('runtime', 'runtime/../../escape', 'docs/probe'):
                with self.assertRaises(ValueError):
                    probe.fresh_output(bad)
            (root / 'runtime').mkdir()
            (root / 'runtime/existing').mkdir()
            with self.assertRaises(ValueError):
                probe.fresh_output('runtime/existing')


if __name__ == '__main__':
    unittest.main()

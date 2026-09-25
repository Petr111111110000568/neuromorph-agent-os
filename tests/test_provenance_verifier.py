"""Synthetic provenance counterexamples; no network, clock access or model calls."""
import copy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone, tzinfo
import hashlib
import unittest
from unittest.mock import patch

from workbench import provenance_verifier as verifier


class ProvenanceVerifierTests(unittest.TestCase):
    def fixture(self, blob=b'public synthetic source'):
        digest = hashlib.sha256(blob).hexdigest()
        record = {'record_id': 'source-1', 'project_id': 'project-a', 'purpose': 'research-evidence',
                  'source_uri': 'urn:fixture:one', 'digest': digest, 'source_ts': '2026-09-25T10:00:00Z'}
        registry = {'source-1': {key: value for key, value in record.items() if key != 'record_id'}}
        registry['source-1'].update(valid_from='2026-09-25T09:00:00Z',
            valid_until='2026-09-26T00:00:00Z', trust=2, revoked=False)
        policy = {'project_id': 'project-a', 'purpose': 'research-evidence',
                  'min_trust': 2, 'max_blob_bytes': 1024}
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        return {'blob': blob, 'record': record, 'registry': registry, 'policy': policy, 'now': now}

    def check(self, inputs):
        return verifier.verify_provenance(**inputs)

    def test_valid_repeatable_check_has_immutable_result_and_does_not_mutate_inputs(self):
        inputs = self.fixture()
        before = copy.deepcopy(inputs)
        first = self.check(inputs)
        self.assertTrue(first.accepted)
        self.assertEqual(first.reason, 'verified_against_trusted_registry')
        self.assertEqual(self.check(inputs), first)  # No anti-replay claim within valid scope.
        self.assertEqual(inputs, before)
        with self.assertRaises(FrozenInstanceError):
            first.accepted = False

    def test_trusted_threshold_cannot_be_lowered_by_untrusted_record(self):
        for untrusted_threshold in (0, -1, 100):
            inputs = self.fixture()
            inputs['record']['required_trust'] = untrusted_threshold
            inputs['registry']['source-1']['trust'] = 1
            result = self.check(inputs)
            self.assertFalse(result.accepted)
            self.assertEqual(result.reason, 'insufficient_trust')
        inputs = self.fixture()
        inputs['record']['required_trust'] = 100
        self.assertTrue(self.check(inputs).accepted)  # Only trusted policy has authority.

    def test_correct_blob_from_other_project_or_purpose_cannot_be_replayed(self):
        for scope in ('project_id', 'purpose'):
            inputs = self.fixture()
            inputs['policy'][scope] = 'another-scope'
            self.assertEqual(self.check(inputs).reason, 'scope_mismatch')
            inputs['record'][scope] = 'another-scope'  # Editing untrusted envelope still fails.
            self.assertEqual(self.check(inputs).reason, 'scope_mismatch')
        inputs = self.fixture()
        inputs['record']['project_id'] = 'another-project'
        self.assertEqual(self.check(inputs).reason, 'scope_mismatch')

    def test_unknown_record_and_independent_digest_tampering_fail(self):
        inputs = self.fixture()
        inputs['record']['record_id'] = 'unknown'
        self.assertEqual(self.check(inputs).reason, 'unknown_record')
        for target in ('blob', 'record', 'registry'):
            inputs = self.fixture()
            if target == 'blob':
                inputs['blob'] += b' changed'
            elif target == 'record':
                inputs['record']['digest'] = '0' * 64
            else:
                inputs['registry']['source-1']['digest'] = '0' * 64
            self.assertEqual(self.check(inputs).reason, 'digest_mismatch')
        for bad in ('A' * 64, 'a' * 63, 'a' * 65, 'g' * 64, b'a' * 64):
            inputs = self.fixture()
            inputs['record']['digest'] = bad
            self.assertFalse(self.check(inputs).accepted)

    def test_uri_is_an_exact_opaque_identifier_and_never_a_fetch_request(self):
        inputs = self.fixture()
        inputs['registry']['source-1']['source_uri'] = 'https://example.invalid/a%2Fb'
        inputs['record']['source_uri'] = 'https://example.invalid/a/b'
        self.assertEqual(self.check(inputs).reason, 'source_mismatch')
        inputs['record']['source_uri'] = inputs['registry']['source-1']['source_uri'] = 'file:///nonexistent/opaque-id'
        # No filesystem is consulted: the caller already supplied all bytes.
        self.assertTrue(self.check(inputs).accepted)

    def test_source_timestamp_cannot_be_rebound_even_to_equivalent_spelling(self):
        for rebound in ('2026-09-25T11:00:00Z', '2026-09-25T10:00:00+00:00'):
            inputs = self.fixture()
            inputs['record']['source_ts'] = rebound
            self.assertEqual(self.check(inputs).reason, 'timestamp_mismatch')

    def test_missing_expiry_revocation_and_required_fields_fail_closed(self):
        for missing in ('valid_until', 'valid_from', 'revoked', 'source_ts', 'purpose'):
            inputs = self.fixture()
            del inputs['registry']['source-1'][missing]
            self.assertFalse(self.check(inputs).accepted)
        inputs = self.fixture()
        inputs['registry']['source-1']['revoked'] = True
        self.assertEqual(self.check(inputs).reason, 'revoked')

    def test_half_open_validity_and_issuance_boundaries(self):
        inputs = self.fixture()
        entry = inputs['registry']['source-1']
        entry['source_ts'] = inputs['record']['source_ts'] = entry['valid_from']
        inputs['now'] = datetime(2026, 9, 25, 9, tzinfo=timezone.utc)
        self.assertTrue(self.check(inputs).accepted)  # Start is included.
        inputs['now'] = datetime(2026, 9, 26, tzinfo=timezone.utc)
        self.assertEqual(self.check(inputs).reason, 'not_current')  # End is excluded.
        inputs['now'] -= timedelta(microseconds=1)
        self.assertTrue(self.check(inputs).accepted)
        for issued in ('2026-09-25T08:59:59Z', '2026-09-26T00:00:00Z'):
            inputs = self.fixture()
            inputs['record']['source_ts'] = inputs['registry']['source-1']['source_ts'] = issued
            self.assertEqual(self.check(inputs).reason, 'invalid_time_window')
        inputs = self.fixture()
        inputs['now'] = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(self.check(inputs).reason, 'not_current')  # Not yet issued.
        inputs = self.fixture()
        inputs['registry']['source-1']['valid_until'] = inputs['registry']['source-1']['valid_from']
        self.assertEqual(self.check(inputs).reason, 'invalid_time_window')

    def test_aware_offsets_work_but_naive_malformed_or_hostile_datetimes_do_not(self):
        inputs = self.fixture()
        inputs['now'] = datetime(2026, 9, 25, 15, tzinfo=timezone(timedelta(hours=3)))
        self.assertTrue(self.check(inputs).accepted)
        inputs['now'] = datetime(2026, 9, 25, 12)
        self.assertFalse(self.check(inputs).accepted)
        for bad in ('2026-09-25T10:00:00', '2026-02-30T10:00:00Z',
                    '2026-09-25T10:00:00+01:99', '2026-09-25 10:00:00Z',
                    '2026-09-25T10:00:00.1234567Z', 'x' * 1000, None, 123):
            inputs = self.fixture()
            inputs['record']['source_ts'] = inputs['registry']['source-1']['source_ts'] = bad
            self.assertFalse(self.check(inputs).accepted)

        class ExplosiveTimezone(tzinfo):
            def utcoffset(self, value):
                raise AssertionError('User-defined timezone callback must not execute')
        inputs = self.fixture()
        inputs['now'] = datetime(2026, 9, 25, 12, tzinfo=ExplosiveTimezone())
        self.assertFalse(self.check(inputs).accepted)

    def test_types_bool_numbers_and_unknown_fields_are_not_coerced(self):
        mutations = [('policy', 'min_trust', True), ('policy', 'min_trust', 2.0),
                     ('policy', 'min_trust', -1), ('policy', 'max_blob_bytes', True),
                     ('policy', 'max_blob_bytes', verifier.MAX_BLOB_BYTES + 1),
                     ('policy', 'project_id', []), ('record', 'required_trust', False),
                     ('record', 'tools', []), ('entry', 'trust', True),
                     ('entry', 'trust', float('nan')), ('entry', 'revoked', 0),
                     ('entry', 'signature', 'unverified claim')]
        for owner, key, value in mutations:
            with self.subTest(owner=owner, key=key, value=value):
                inputs = self.fixture()
                target = inputs['registry']['source-1'] if owner == 'entry' else inputs[owner]
                target[key] = value
                self.assertFalse(self.check(inputs).accepted)
        for key in ('blob', 'record', 'registry', 'policy', 'now'):
            inputs = self.fixture()
            inputs[key] = None
            self.assertFalse(self.check(inputs).accepted)

        class MappingSubclass(dict):
            def __getitem__(self, key):
                raise AssertionError('Hostile mapping must not execute')
        inputs = self.fixture()
        inputs['record'] = MappingSubclass(inputs['record'])
        self.assertFalse(self.check(inputs).accepted)

    def test_size_limit_is_checked_before_hashing_and_exact_limit_is_valid(self):
        inputs = self.fixture(b'abcd')
        inputs['policy']['max_blob_bytes'] = 4
        self.assertTrue(self.check(inputs).accepted)
        inputs['blob'] = b'abcde'
        with patch.object(verifier.hashlib, 'sha256', side_effect=AssertionError('Oversized input hashed')) as hash_call:
            self.assertEqual(self.check(inputs).reason, 'payload_too_large')
        hash_call.assert_not_called()
        inputs = self.fixture(b'')
        inputs['policy']['max_blob_bytes'] = 1
        self.assertTrue(self.check(inputs).accepted)  # Empty bytes have a well-defined digest.

    def test_identifier_uri_and_registry_bounds_are_enforced(self):
        inputs = self.fixture()
        entry = inputs['registry'].pop('source-1')
        record_id = 'r' * verifier.MAX_ID_CHARS
        inputs['registry'][record_id] = entry
        inputs['record']['record_id'] = record_id
        inputs['record']['source_uri'] = entry['source_uri'] = 'u' * verifier.MAX_URI_CHARS
        self.assertTrue(self.check(inputs).accepted)
        inputs['record']['source_uri'] += 'x'
        self.assertFalse(self.check(inputs).accepted)
        inputs = self.fixture()
        inputs['record']['record_id'] = 'r' * (verifier.MAX_ID_CHARS + 1)
        self.assertFalse(self.check(inputs).accepted)
        inputs = self.fixture()
        inputs['registry'] = {str(n): {} for n in range(verifier.MAX_REGISTRY_ENTRIES + 1)}
        self.assertFalse(self.check(inputs).accepted)


if __name__ == '__main__':
    unittest.main()


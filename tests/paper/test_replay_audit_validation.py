"""Warnings cannot hide protocol errors or cross-policy evidence mixing."""
from copy import deepcopy
import unittest

from ae.repro.replay_audit import summarize, validate_stats
from ae.repro.analysis import fresh_labels


class ReplayAuditValidationTests(unittest.TestCase):
    def test_audit_differences_complete_but_do_not_claim_exact_messages(self):
        report = dict(ok=True, schema_version=1, message_policy='audit',
                      stats=dict(message_policy='audit', n_mismatch=2, n_protocol_errors=0,
                                 audit_records_dropped=1, audit_payloads_omitted=1))
        summary = summarize([report], 'audit')
        self.assertEqual(summary['message_equivalence'], 'different')
        self.assertEqual(summary['n_mismatch'], 2)
        self.assertEqual(summary['audit_records_dropped'], 1)
        self.assertEqual(summary['audit_payloads_omitted'], 1)
        broken = deepcopy(report)
        broken['stats']['n_protocol_errors'] = 1
        with self.assertRaisesRegex(ValueError, 'protocol'):
            summarize([broken], 'audit')

    def test_strict_mismatch_and_missing_or_negative_counters_fail(self):
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            validate_stats(dict(message_policy='strict', n_mismatch=1), 'strict')
        for value in (None, -1, True, '0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_stats(dict(message_policy='audit', n_mismatch=value), 'audit')

    def test_unknown_policy_and_cross_policy_evidence_fail(self):
        for requested in ('audit', 'loose'):
            with self.assertRaises(ValueError):
                validate_stats(dict(message_policy='strict', n_mismatch=0), requested)
        self.assertNotEqual(fresh_labels(dict(message_policy='strict'))['cohort'],
                            fresh_labels(dict(message_policy='audit'))['cohort'])


if __name__ == '__main__':
    unittest.main()

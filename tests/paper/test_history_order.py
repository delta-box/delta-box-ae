"""Producer-side compatibility and optional strict replay validation."""
from __future__ import annotations

import unittest
from unittest import mock

from replay import history_order


class HistoryOrderTests(unittest.TestCase):
    def test_audit_unknown_spans_preserve_live_content_without_logging(self):
        spans = ['new-span', 'new-span']
        with mock.patch.object(history_order, '_runtime_table', return_value=({}, 'table', 'trace')), \
                mock.patch.dict('os.environ', {'MOCK_MESSAGE_POLICY': 'audit'}), \
                mock.patch('builtins.print', side_effect=AssertionError('hot-path print')), \
                mock.patch.object(history_order.json, 'dumps', side_effect=AssertionError('hot-path serialization')):
            self.assertEqual(history_order.restore_span_order('new.py', spans), spans)


if __name__ == '__main__':
    unittest.main()

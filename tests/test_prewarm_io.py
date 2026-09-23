"""External prefetch never overwrites concurrent target mutations."""
import errno
import unittest
from unittest.mock import patch, Mock, call
from backends.deltabox.gsd import prewarm as pw


class PrewarmIoTests(unittest.TestCase):
    def run_warm(self, reads=(b'a', b'b')):
        opened, read, write, close = Mock(return_value=42), Mock(side_effect=reads), Mock(), Mock()
        with patch.dict(pw.os.environ, {'DELTABOX_PREWARM_MODE': 'read'}), \
             patch.object(pw, '_read_rss_mb', return_value=1), \
             patch.object(pw, '_classify_tiered_ranges', return_value=[(1, [(4096, 12288)])]), \
             patch.object(pw, '_log_result'), patch.object(pw.os, 'open', opened), \
             patch.object(pw.os, 'pread', read), patch.object(pw.os, 'pwrite', write), \
             patch.object(pw.os, 'close', close):
            result = pw.prewarm_external(100)
        opened.assert_called_once_with('/proc/100/mem', pw.os.O_RDONLY)
        close.assert_called_once_with(42)
        write.assert_not_called()
        return result, read

    def test_one_read_per_page_and_no_cow_claim(self):
        result, read = self.run_warm()
        self.assertEqual(read.call_args_list, [call(42, 1, 4096), call(42, 1, 8192)])
        self.assertEqual((result['pages'], result['skipped']), (2, 0))
        self.assertFalse(result['write_cow'])
        self.assertEqual(result['mode'], 'read')

    def test_agent_mutation_between_accesses_is_preserved(self):
        target = {4096: b'a', 8192: b'b'}
        def concurrent_read(fd, size, addr):
            observed = target[addr]
            target[addr] = b'new agent value'
            return observed
        self.run_warm(concurrent_read)
        self.assertEqual(set(target.values()), {b'new agent value'})

    def test_unmapped_and_empty_reads_are_skipped(self):
        result, _ = self.run_warm([OSError(errno.EIO, 'unmapped'), b''])
        self.assertEqual((result['pages'], result['skipped']), (0, 2))

    def test_unsafe_and_unknown_modes_rejected_before_thread_or_mem_access(self):
        for mode in ('write', 'wrtie', ''):
            with self.subTest(mode=mode), patch.dict(pw.os.environ, {'DELTABOX_PREWARM_MODE': mode}), \
                 patch.object(pw.os, 'open') as opened, patch.object(pw.threading, 'Thread') as thread:
                with self.assertRaisesRegex(ValueError, 'Unsafe or unknown'):
                    pw.prewarm_external(100)
                with self.assertRaisesRegex(ValueError, 'Unsafe or unknown'):
                    pw.spawn_prewarm(100)
                opened.assert_not_called()
                thread.assert_not_called()

    def test_unset_mode_defaults_to_read(self):
        with patch.dict(pw.os.environ, {}, clear=True):
            self.assertEqual(pw.validate_prewarm_mode(), 'read')


if __name__ == '__main__':
    unittest.main()
